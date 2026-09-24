#!/home/jgo/usr/venv-3.11/bin/python
"""cache_v21/update_daily_rc2f.py - D2: the rc2f contract inside the daily update (helpers called by update_daily.py).

The fixed snapshot rc2f2 (publish 20260921T0119Z-rc2f2) = vendor tables + source-level overrides (overrides/rc2f/**) + rules.
A daily build regenerates the vendor layers (raw window, factors, lifecycle, lineage, events, ledger) and must land on the
same contract, so every override is re-applied to the fresh tables (idempotent: the apply log of the day is compared with the
previous build's log; a regression = blocking gate), and every changed returns cell of the day must be attributed to a
known cause (refetch window, price/factor revision, ledger revision, override status change, lineage/lifecycle change,
new id) - unattributed cells = blocking gate (rc2f_recount rule).

Pieces (each called from one U-step of update_daily.py):
  overrides   load_overrides / prev_apply_log / compare_apply_logs (gate G-OVR)
  price       price_windows / apply_price_to_rows (fresh rows inside an override window) / price_override_integrity (gate G-PRICE-OVR)
  split       reapply_split_overrides (U3, on the vendor-rebuilt table; the vendor table is kept as T2_work/split_vendor.parquet for G-SPLIT)
  lineage     merge_lineage_daily (previous v2 rows carried, new v1 pairs appended) / reapply_lineage_overrides (U4)
  lifecycle   finalize_master_rc2f (U5 after T6: lifecycle overrides on the fresh lifecycle/events/dates/lineage, aux rows appended,
              events delta with the previous R3-reclassed rows carried, settlementReturnScope labels, revisions)
  aux         reapply_aux_additions (new aux ids only; existing ones are carried with their prices refreshed by the U2/U3 fetch)
  ledger      run_ledger_incremental (D1 CLI `ledger_build.py --incremental`), ledger_revisions_summary
  returns     run_returns_qa (returns_v2_qa gates on the stage), attribute_returns_changes (gate G-RET-ATTR)
  exposure    exposure_daily (qa/exposure_daily.md; live book cross; gate G-EXPO, blocking with --strict-exposure)
  publish     build_publish_view (stage view + rc2 stage for publish.py --rc2-stage), anchor_check (engine export anchors vs panel)
"""
from __future__ import annotations

import glob
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import rc2f_apply as ra  # noqa: E402
import rc2f_recount as rr  # noqa: E402

log = logging.getLogger("cache_v21.update.rc2f")

OVERRIDES_GLOB_DEFAULT = str(HERE / "overrides" / "rc2f" / "*" / "*.csv")
APPLIED_STATUSES = ("applied", "applied_unverified", "applied_unchecked", "applied_no_effect", "no_op", "carried")
QF_PRICE = (ra.QF_PRICE_OVERRIDE, ra.QF_PRICE_OVERRIDE_EST)
QF_POST_LIFECYCLE = 4
BOOK_GLOB = "/home/base/portfolio/portfolio_v1_0/combo/combo_export/combo000014/*_pos_values.parquet"
LEDGER_TOL_DAYS = rr.LEDGER_ROW_TOL_DAYS          # 10: cells after a changed ledger row (settlement-bar alignment, marker window)
LINEAGE_TOL_DAYS = rr.LINEAGE_ROW_TOL_DAYS        # 45: cells around a changed lineage link / lifecycle end
FACTOR_TOL_DAYS = 10


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(32 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _days(x) -> np.ndarray:
    return np.asarray(pd.to_datetime(np.asarray(x))).astype("datetime64[D]").astype(np.int64)


def _keys(ids, days) -> np.ndarray:
    return rr._keys(ids, days)


# ======================================================================================
# overrides: load (parsed like rc2f_apply.load_all_overrides, without the rc2-build validator), previous log, status gate
# ======================================================================================
class LightRef:
    """Duck-typed rc2f_apply.Ref for _common_checks: the id universe of the build (instruments + aux) and the ledger ids."""

    def __init__(self, ids, ledger_ids=None, ledger: pd.DataFrame | None = None):
        self.ids = set(int(i) for i in ids)
        self.ledger_ids = set(int(i) for i in (ledger_ids or []))
        self.ledger = ledger
        self.pending_aux_ids: set = set()
        self.pending_lineage_ratios: dict = {}
        self.pending_price_ids: set = set()


def load_overrides(pattern: str, ids, ledger_ids=None) -> tuple[dict, list[dict], list[dict]]:
    """-> ({kind: parsed frame (x_tid, x_isEstimate, x_row, x_file)}, files [{path, kind, sha256, rows}], issues).
    The schema validator of rc2f_apply (validate against the rc2 build) is not re-run daily: the CSVs are the published ones
    (sha-identical to qa/rc2f/overrides of the previous build unless new files were added); only the common checks run."""
    paths = sorted(set(glob.glob(pattern))) if pattern else []
    paths = [p for p in paths if ra.kind_of(p)]
    ref = LightRef(ids, ledger_ids)
    by_kind = {k: [] for k in ra.KINDS}
    files, issues = [], []
    for p in paths:
        kind = ra.kind_of(p)
        df = ra.read_override_csv(p)
        files.append({"path": str(p), "kind": kind, "sha256": sha256(Path(p)), "rows": int(len(df))})
        if not len(df):
            continue
        iss = ra.Issues()
        parsed = ra._common_checks(df, kind, ref, iss)
        issues += [r for r in iss.rows if r["level"] == "ERROR" and "not in base_data/master" not in r["message"]]
        if parsed is None:
            issues.append({"level": "ERROR", "file": p, "row": 1, "overrideId": "", "message": "unparsable (missing columns)"})
            continue
        by_kind[kind].append(parsed)
    ovr = {k: (pd.concat(v, ignore_index=True) if v else pd.DataFrame()) for k, v in by_kind.items()}
    return ovr, files, issues


def prev_apply_log(prev_root: Path, layout: str) -> pd.DataFrame | None:
    """Apply log of the previous build: published qa/rc2f/rc2f_apply_log.csv, stage rc2f/rc2f_apply_log.csv."""
    cands = [Path(prev_root) / "rc2f" / "rc2f_apply_log.csv", Path(prev_root) / "qa" / "rc2f" / "rc2f_apply_log.csv"]
    if layout == "published":
        cands.reverse()
    for p in cands:
        if p.exists():
            return pd.read_csv(p, dtype=str, keep_default_na=False)
    return None


def compare_apply_logs(cur: pd.DataFrame, prev: pd.DataFrame | None) -> dict:
    """G-OVR: every override that was applied (any applied_* / no_op) in the previous build must not be rejected now; a status
    change (e.g. applied -> no_op: the vendor table no longer holds the boundary) is listed (warning), new ids are listed."""
    res = {"rows": int(len(cur)), "status": cur.status.value_counts().to_dict() if len(cur) else {}, "rejected": [], "regressions": [], "status_changed": [],
           "new_ids": [], "missing_from_current": [], "prev_available": prev is not None}
    if len(cur):
        rej = cur[cur.status == "rejected"]
        res["rejected"] = [{"overrideId": r.overrideId, "kind": r.kind, "reason": r.reason[:160]} for r in rej.itertuples()]
    if prev is None or not len(cur):
        res["pass"] = not res["rejected"]
        return res
    p = prev.set_index("overrideId").status.to_dict()
    c = cur.set_index("overrideId")
    for oid, row in c.iterrows():
        ps = p.get(oid)
        if ps is None:
            res["new_ids"].append(oid)
            continue
        cs = row.status
        if cs == ps:
            continue
        rec = {"overrideId": oid, "kind": row.kind, "prev": ps, "now": cs, "reason": str(row.reason)[:160]}
        if cs == "rejected" and ps in APPLIED_STATUSES:
            res["regressions"].append(rec)
        elif cs == "carried" and ps in APPLIED_STATUSES:
            continue                                   # daily carry of an already-applied override = same state
        else:
            res["status_changed"].append(rec)
    res["missing_from_current"] = sorted(o for o in set(p) - set(c.index) if not str(o).startswith("RULE-"))     # RULE-* rows are re-evaluated on the day's tables
    res["pass"] = not res["regressions"] and not res["rejected"] and not res["missing_from_current"]
    return res


# ======================================================================================
# price overrides: windows, application to fresh rows, integrity of the stored rows
# ======================================================================================
def price_windows(ovr_price: pd.DataFrame) -> pd.DataFrame:
    cols = ["tradingItemId", "d0", "d1", "op", "value", "fields", "isEstimate", "overrideId", "caseId"]
    if ovr_price is None or not len(ovr_price):
        return pd.DataFrame(columns=cols)
    rows = []
    for r in ovr_price.itertuples(index=False):
        if r.x_tid is None:
            continue
        fields = [f.strip() for f in str(r.fields).split("|") if f.strip()] if str(r.fields).strip() else list(ra.PRICE_FIELDS)
        rows.append({"tradingItemId": int(r.x_tid), "d0": pd.Timestamp(ra.parse_date(r.dateFrom)), "d1": pd.Timestamp(ra.parse_date(r.dateTo)), "op": r.op.strip(),
                     "value": float(ra.parse_float(r.value)), "fields": fields, "isEstimate": bool(r.x_isEstimate), "overrideId": r.overrideId, "caseId": r.caseId})
    return pd.DataFrame(rows, columns=cols)


def rows_in_windows(ids, dates, win: pd.DataFrame) -> np.ndarray:
    """bool per row: (id, date) inside any price override window."""
    ids = np.asarray(ids, dtype=np.int64)
    d = np.asarray(pd.to_datetime(np.asarray(dates))).astype("datetime64[ns]")
    m = np.zeros(len(ids), dtype=bool)
    if not len(win):
        return m
    for r in win.itertuples(index=False):
        m |= (ids == r.tradingItemId) & (d >= np.datetime64(r.d0, "ns")) & (d <= np.datetime64(r.d1, "ns"))
    return m


def apply_price_to_rows(df: pd.DataFrame, win: pd.DataFrame, fresh: np.ndarray, alog: ra.ApplyLog | None = None) -> tuple[pd.DataFrame, int]:
    """Apply the price overrides to the FRESH rows (vendor values pulled today) that fall inside an override window: multiply /
    set_close on the override's fields, qualityFlag 8 (32 when isEstimate). Rows kept from the stored file already carry the
    override. Returns (frame, n rows changed)."""
    if not len(win) or not len(df) or not fresh.any():
        return df, 0
    df = df.copy()
    ids = df.tradingItemId.to_numpy(np.int64)
    d = np.asarray(pd.to_datetime(df.date)).astype("datetime64[ns]")
    q = df.qualityFlag.to_numpy().copy()
    n = 0
    for r in win.itertuples(index=False):
        m = fresh & (ids == r.tradingItemId) & (d >= np.datetime64(r.d0, "ns")) & (d <= np.datetime64(r.d1, "ns"))
        if not m.any():
            continue
        for f in r.fields:
            if f not in df.columns:
                continue
            v = df[f].to_numpy(dtype=np.float64).copy()
            if r.op == "multiply":
                v[m] = v[m] * r.value
            elif r.op == "set_close" and f == "close":
                v[m] = r.value
            df[f] = v.astype(df[f].dtype) if df[f].dtype.kind == "f" else v
        q[m] = ra.QF_PRICE_OVERRIDE_EST if r.isEstimate else ra.QF_PRICE_OVERRIDE
        n += int(m.sum())
        if alog is not None:
            alog.set(r.overrideId, caseId=r.caseId, kind="price", action=r.op, status="applied", reason=f"applied to {int(m.sum())} fresh row(s) of the day",
                     affected_rows=int(m.sum()), target=str(r.tradingItemId), file="T2/base_data.parquet")
    df["qualityFlag"] = q.astype(np.int8)
    return df, n


def price_override_integrity(base_path: Path, win: pd.DataFrame, read_rows) -> dict:
    """G-PRICE-OVR: every stored row inside a price override window carries qualityFlag 8/32 (or 4 = post-lifecycle tail, kept by
    publish); rows in the window with another flag = the override was lost. read_rows(path, cols, ids) is update_daily.read_rows_filtered."""
    if not len(win):
        return {"pass": True, "windows": 0, "rows_in_windows": 0, "bad_rows": 0}
    ids = np.array(sorted(set(win.tradingItemId.astype(int))), dtype=np.int64)
    rows = read_rows(base_path, ["tradingItemId", "date", "close", "qualityFlag"], ids=ids)
    inw = rows_in_windows(rows.tradingItemId.to_numpy(), rows.date.to_numpy(), win)
    sub = rows[inw]
    ok = sub.qualityFlag.isin(list(QF_PRICE) + [QF_POST_LIFECYCLE])
    bad = sub[~ok]
    per = []
    for r in win.itertuples(index=False):
        m = (sub.tradingItemId == r.tradingItemId) & (sub.date >= r.d0) & (sub.date <= r.d1)
        per.append({"overrideId": r.overrideId, "tradingItemId": r.tradingItemId, "rows": int(m.sum()), "flag8_32": int(sub[m].qualityFlag.isin(list(QF_PRICE)).sum()),
                    "flag4": int((sub[m].qualityFlag == QF_POST_LIFECYCLE).sum()), "other": int((~sub[m].qualityFlag.isin(list(QF_PRICE) + [QF_POST_LIFECYCLE])).sum())})
    return {"pass": len(bad) == 0, "windows": int(len(win)), "rows_in_windows": int(len(sub)), "bad_rows": int(len(bad)),
            "bad_examples": [{"tradingItemId": int(t), "date": str(pd.Timestamp(d).date()), "qualityFlag": int(q)} for t, d, q in zip(bad.tradingItemId.head(10), bad.date.head(10), bad.qualityFlag.head(10))],
            "per_override": per}


# ======================================================================================
# split overrides (U3): re-apply on the vendor-rebuilt table
# ======================================================================================
def calendar_from_frame(cal_df: pd.DataFrame):
    """ledger_build.Calendar from the build's calendar.parquet frame (isTradingDay rows)."""
    import ledger_build as lb
    days = cal_df.loc[cal_df.isTradingDay, "date"].to_numpy().astype("datetime64[D]")
    return lb.Calendar(days)


def reapply_split_overrides(sp_vendor: pd.DataFrame, ovr_split: pd.DataFrame, base_path: Path, cal, alog: ra.ApplyLog, price_by_tid: dict | None = None,
                            carried_ids=None) -> tuple[pd.DataFrame, dict]:
    """rc2f_apply.apply_split_overrides on the vendor-rebuilt split table (one table for instruments + aux ids). closes = raw closes
    of the override ids from the merged base_data of the day (the 3-state raw-jump check runs again). carried_ids (P0-1): ids whose
    intervals were carried from the previous build (kept restatement) already hold their overrides -> status carried, not re-applied."""
    import ledger_build as lb
    if ovr_split is None or not len(ovr_split):
        return sp_vendor, {"overrides": 0, "changed": False}
    sp = sp_vendor.copy()
    carried = set(int(i) for i in (carried_ids or []))
    todo = ovr_split
    if carried:
        m = ovr_split.x_tid.map(lambda v: v is not None and int(v) in carried)
        for r in ovr_split[m].itertuples(index=False):
            alog.set(r.overrideId, caseId=r.caseId, kind="split", action=r.action.strip(), status="carried", reason="factor intervals carried with the kept raw (vendor restatement not adopted)",
                     affected_rows=0, target=f"{int(r.x_tid)}@{r.exDate}", file="T2")
        todo = ovr_split[~m]
        if not len(todo):
            return sp, {"overrides": int(len(ovr_split)), "changed": False, "carried": int(m.sum())}
    ids = set(int(i) for i in todo.x_tid.dropna().astype(int))
    closes = lb.RawCloses([base_path], ids)
    res = ra.apply_split_overrides(todo, {"T2": (sp, set(sp.tradingItemId.astype(int)))}, alog, cal, closes, price_by_tid or {})
    df_new, changed = res["T2"]
    df_new = df_new.sort_values(["tradingItemId", "validFrom"]).reset_index(drop=True)
    lg = alog.frame()
    lg = lg[lg.kind == "split"] if len(lg) else lg
    return df_new, {"overrides": int(len(ovr_split)), "changed": bool(changed), "status": lg.status.value_counts().to_dict() if len(lg) else {},
                    "rows_vendor": int(len(sp_vendor)), "rows": int(len(df_new))}


# ======================================================================================
# lineage (U4): previous v2 rows carried + new v1 pairs; overrides re-applied (idempotent daily rule)
# ======================================================================================
V2_COLS = ["oldId", "newId", "relation", "effectiveDate", "ratio", "evidence", "confidence", "ratioSource", "linkClass", "reviewFlag", "sources", "version",
           "chainGapTradingDays", "chainable", "linkVerified"]
LINK_CLASS = {"rename": "simple_rename", "relist": "relist", "merger": "merger", "spinoff_parent": "spin_off"}


def merge_lineage_daily(prev_ln: pd.DataFrame, v1_new: pd.DataFrame, lifecycle: pd.DataFrame, cal_days: np.ndarray, max_gap: int = 5,
                        recent_after: pd.Timestamp | None = None, new_ids=None, excluded_out: Path | None = None) -> tuple[pd.DataFrame, dict]:
    """Daily lineage = previous build's lineage (v2 + overrides, carried row by row) + RECENT pairs of today's v1 candidate set that are new
    (v2 extras filled from the v1 row, version 'v1-daily', chainable by the R3 formula; ratio NaN -> not chainable -> first row NaN).
    A new pair is recent when its effectiveDate >= recent_after, or the new id is a new instrument, or either id's lastTradeDate >= recent_after;
    historical pairs that surface only because the daily candidate source differs from the snapshot's (the build's own events file vs the
    v1 events file of the T1 build) are excluded and listed (excluded_out) - the fixed snapshot's lineage is not re-litigated by the daily.
    chainGapTradingDays is recomputed for every rename/relist row from today's lifecycle; chainable is recomputed by the formula only for
    rows no override touched (version != 'rc2f' and ratioSource not rc2f_*), the others keep their value."""
    import master_lifecycle as ml
    ln = prev_ln.copy()
    for c in V2_COLS:
        if c not in ln.columns:
            ln[c] = False if c in ("chainable", "linkVerified") else ("" if c not in ("chainGapTradingDays", "ratio", "effectiveDate") else pd.NA)
    key_prev = set(zip(ln.oldId.astype(int), ln.newId.astype(int)))
    k_lc = lifecycle.set_index("tradingItemId")
    new_ids = set(int(i) for i in (new_ids or []))
    rows, excluded = [], []
    for r in v1_new.itertuples(index=False):
        k = (int(r.oldId), int(r.newId))
        if k in key_prev:
            continue
        rel = str(r.relation)
        if recent_after is not None:
            eff = pd.Timestamp(r.effectiveDate) if pd.notna(r.effectiveDate) else pd.NaT
            lo = k_lc.lastTradeDate.get(k[0], pd.NaT)
            old_ended = str(k_lc.status.get(k[0], "")) != "active" if "status" in k_lc.columns else True
            recent = (pd.notna(eff) and eff >= recent_after) or k[1] in new_ids or k[0] in new_ids \
                or (old_ended and pd.notna(lo) and pd.Timestamp(lo) >= recent_after)          # the old line ended recently (an active acquirer is not 'recent')
            if not recent:
                excluded.append({"oldId": k[0], "newId": k[1], "relation": rel, "effectiveDate": eff, "ratio": r.ratio, "confidence": r.confidence, "evidence": str(r.evidence)[:200],
                                 "reason": "historical pair not in the snapshot lineage (candidate source differs); review, not auto-added"})
                continue
        rows.append({"oldId": k[0], "newId": k[1], "relation": rel, "effectiveDate": pd.Timestamp(r.effectiveDate) if pd.notna(r.effectiveDate) else pd.NaT,
                     "ratio": float(r.ratio) if pd.notna(r.ratio) else np.nan, "evidence": str(r.evidence)[:600], "confidence": str(r.confidence),
                     "ratioSource": "events" if pd.notna(r.ratio) else "", "linkClass": LINK_CLASS.get(rel, rel), "reviewFlag": "", "sources": str(getattr(r, "sources", "") or ""),
                     "version": "v1-daily", "chainGapTradingDays": pd.NA, "chainable": False, "linkVerified": False})
    n_new = len(rows)
    if rows:
        ln = pd.concat([ln, pd.DataFrame(rows)[ln.columns]], ignore_index=True)
    if excluded_out is not None:
        pd.DataFrame(excluded, columns=["oldId", "newId", "relation", "effectiveDate", "ratio", "confidence", "evidence", "reason"]).to_csv(excluded_out, index=False)
    key_v1 = set(zip(v1_new.oldId.astype(int), v1_new.newId.astype(int))) if len(v1_new) else set()
    not_in_v1 = [k for k in key_prev if k not in key_v1]
    # chain gap / chainable (recomputed again in finalize with the final lifecycle: carried first dates)
    ln = recompute_chain_gaps(ln, lifecycle, cal_days, max_gap)
    ln["linkVerified"] = ln.linkVerified.fillna(False).astype(bool)
    ln["effectiveDate"] = pd.to_datetime(ln.effectiveDate).astype("datetime64[ns]")
    ln["oldId"] = ln.oldId.astype("int64")
    ln["newId"] = ln.newId.astype("int64")
    ln["ratio"] = ln.ratio.astype("float64")
    for c in ("relation", "evidence", "confidence", "ratioSource", "linkClass", "reviewFlag", "sources", "version"):
        ln[c] = ln[c].fillna("").astype(str)
    ln = ln[V2_COLS].sort_values(["oldId", "newId"]).reset_index(drop=True)
    if ln.duplicated(["oldId", "newId"]).any():
        raise RuntimeError("daily lineage has duplicate (oldId, newId) pairs")
    prev_chain = prev_ln.set_index(["oldId", "newId"]).chainable.astype(bool) if "chainable" in prev_ln.columns else None
    chg = 0
    if prev_chain is not None:
        cur_chain = ln.set_index(["oldId", "newId"]).chainable
        common = prev_chain.index.intersection(cur_chain.index)
        chg = int((prev_chain.loc[common] != cur_chain.loc[common]).sum())
    return ln, {"rows_prev": int(len(prev_ln)), "rows": int(len(ln)), "new_pairs": n_new, "new_pairs_excluded_historical": len(excluded), "prev_pairs_not_in_v1_today": len(not_in_v1),
                "chainable_changed_vs_prev": chg, "chainable_true": int(ln.chainable.sum()), "linkVerified_true": int(ln.linkVerified.sum())}


def recompute_chain_gaps(ln: pd.DataFrame, lifecycle: pd.DataFrame, cal_days: np.ndarray, max_gap: int = 5, prev_ln: pd.DataFrame | None = None,
                         lc_prev: pd.DataFrame | None = None) -> pd.DataFrame:
    """chainGapTradingDays of every rename/relist row from the given lifecycle (old lastTradeDate -> new firstTradeDate); chainable by the
    R3 formula only for rows no override touched (version != rc2f and ratioSource not rc2f_*), the others keep their value.
    With prev_ln / lc_prev: a pair whose two inputs are unchanged vs the previous build keeps the previous gap and chainable (the snapshot's
    gaps were computed on another trading-day list; they are not re-litigated by the daily)."""
    import master_lifecycle as ml
    ln = ln.copy()
    k = lifecycle.set_index("tradingItemId")
    kp = lc_prev.set_index("tradingItemId") if lc_prev is not None else None
    pg = prev_ln.set_index(["oldId", "newId"]) if prev_ln is not None and len(prev_ln) else None
    gaps, chain = [], []
    for r in ln.itertuples(index=False):
        if r.relation not in ("rename", "relist"):
            gaps.append(pd.NA if pd.isna(r.chainGapTradingDays) else int(r.chainGapTradingDays))
            chain.append(bool(r.chainable))
            continue
        lo, fn = k.lastTradeDate.get(r.oldId, pd.NaT), k.firstTradeDate.get(r.newId, pd.NaT)
        if pg is not None and kp is not None and (r.oldId, r.newId) in pg.index and r.oldId in kp.index and r.newId in kp.index:
            lo_p, fn_p = kp.lastTradeDate.get(r.oldId, pd.NaT), kp.firstTradeDate.get(r.newId, pd.NaT)
            same = ((pd.isna(lo) and pd.isna(lo_p)) or (pd.notna(lo) and pd.notna(lo_p) and pd.Timestamp(lo) == pd.Timestamp(lo_p))) and \
                   ((pd.isna(fn) and pd.isna(fn_p)) or (pd.notna(fn) and pd.notna(fn_p) and pd.Timestamp(fn) == pd.Timestamp(fn_p)))
            if same:
                g0 = pg.at[(r.oldId, r.newId), "chainGapTradingDays"]
                gaps.append(pd.NA if pd.isna(g0) else int(g0))
                chain.append(bool(pg.at[(r.oldId, r.newId), "chainable"]))
                continue
        if pd.isna(lo) or pd.isna(fn):
            gap = pd.NA if pd.isna(r.chainGapTradingDays) else int(r.chainGapTradingDays)
        elif fn <= lo:
            gap = 0
        else:
            gap = int(ml.tdays_between(cal_days, np.array([lo], dtype="datetime64[ns]"), np.array([fn], dtype="datetime64[ns]"))[0]) - 1
        gaps.append(gap)
        touched = str(r.version) == "rc2f" or str(r.ratioSource).startswith("rc2f")
        if touched:
            chain.append(bool(r.chainable))
        else:
            chain.append(bool(str(r.confidence) in ("high", "medium") and pd.notna(r.ratio) and not pd.isna(gap) and int(gap) <= max_gap))
    ln["chainGapTradingDays"] = pd.array(gaps, dtype="Int64")
    ln["chainable"] = np.array(chain, dtype=bool)
    return ln


def reapply_lineage_overrides(ovr: pd.DataFrame, ln: pd.DataFrame, lc_all: pd.DataFrame, cal_days: np.ndarray, alog: ra.ApplyLog) -> pd.DataFrame:
    """Idempotent daily rule per override (judged on the current merged table): drop -> apply when the pair is present, else carried;
    add -> carried when present with ratioSource rc2f_*:<oid>, apply when absent, replace when present as a plain v1/daily pair;
    set_ratio -> carried when ratioSource ends with ':<oid>', apply when present otherwise, rejected when absent (as rc2f_apply);
    set_chainable -> carried when chainable already equals the value. The apply itself is rc2f_apply.apply_lineage_overrides."""
    if ovr is None or not len(ovr):
        return ln
    todo = []
    for i, r in enumerate(ovr.itertuples(index=False)):
        old, new = ra.parse_int(r.oldId), ra.parse_int(r.newId)
        act, oid = r.action.strip(), r.overrideId
        m = (ln.oldId == old) & (ln.newId == new)
        present = bool(m.any())
        row = ln[m].iloc[0] if present else None
        carried = None
        if act == "drop":
            carried = not present
        elif act == "add":
            want = ra.parse_float(r.ratio)
            same_ratio = present and ((pd.isna(row.ratio) and want is None) or (want is not None and pd.notna(row.ratio) and abs(float(row.ratio) - float(want)) <= 1e-12))
            if present and str(row.ratioSource).startswith("rc2f") and same_ratio:      # added earlier (a later lifecycle override may have relabelled ratioSource)
                carried = True
            elif present:
                ln = ln[~m]                                            # plain pair re-created by today's v1: replaced by the override row
                carried = False
            else:
                carried = False
        elif act == "set_ratio":
            want = ra.parse_float(r.ratio)
            same_ratio = present and ((pd.isna(row.ratio) and want is None) or (want is not None and pd.notna(row.ratio) and abs(float(row.ratio) - float(want)) <= 1e-12))
            carried = present and str(row.ratioSource).startswith("rc2f") and same_ratio      # applied earlier (ratioSource rc2f_*[:<id>]; a later lifecycle override may have relabelled it)
        elif act == "set_chainable":
            v = ra.parse_bool(r.chainable) if str(r.chainable).strip() else None
            carried = present and v is not None and bool(row.chainable) == bool(v)
        if carried:
            alog.set(oid, caseId=r.caseId, kind="lineage", action=act, status="carried", reason="already in the carried lineage", affected_rows=0,
                     target=f"{old}->{new}", file="lineage")
        else:
            todo.append(i)
    if todo:
        ln = ra.apply_lineage_overrides(ovr.iloc[todo], ln, lc_all, cal_days, alog)
    return ln.sort_values(["oldId", "newId"]).reset_index(drop=True)


# ======================================================================================
# lifecycle / events / dates (U5, after T6): overrides re-applied on the fresh tables, aux rows appended, revisions
# ======================================================================================
def events_daily_merge(fresh: pd.DataFrame, prev: pd.DataFrame | None) -> tuple[pd.DataFrame, dict]:
    """Fresh T6 events of the day + the previous build's rows that were produced by the rc2 R3 reclassification or an rc2f override
    (overrideSource / source reclass_* / endDateSource override): those rows are carried (historical, corrected), the rest follow
    the vendor. Rows of the previous build whose id is not in today's events are carried too (end dates of ended lines)."""
    ev = fresh.copy()
    if "settlementReturnScope" not in ev.columns:
        ev["settlementReturnScope"] = "full"
    info = {"fresh_rows": int(len(fresh)), "carried_rows": 0, "carried_ids_not_in_fresh": 0, "fresh_rows_differ_from_carried": 0}
    if prev is None or not len(prev):
        return ra._events_types(ev), info
    prev = prev.copy()
    if "settlementReturnScope" not in prev.columns:
        prev["settlementReturnScope"] = "full"
    patched = prev.overrideSource.fillna("").astype(str).ne("") | prev.source.fillna("").astype(str).str.startswith("reclass_") | prev.endDateSource.fillna("").astype(str).eq("override")
    carry = prev[patched]
    keep = ev[~ev.tradingItemId.isin(carry.tradingItemId)]
    # fresh rows that differ from the carried ones (visibility: the vendor now says something else for a corrected id)
    f = ev[ev.tradingItemId.isin(carry.tradingItemId)].set_index("tradingItemId")
    c = carry.set_index("tradingItemId")
    common = f.index.intersection(c.index)
    diff = 0
    for tid in common:
        a, b = f.loc[tid], c.loc[tid]
        if pd.Timestamp(a.endDate) != pd.Timestamp(b.endDate) or str(a.actionCode) != str(b.actionCode):
            diff += 1
    missing = prev[~prev.tradingItemId.isin(ev.tradingItemId) & ~patched]
    out = pd.concat([keep, carry[keep.columns], missing[keep.columns]], ignore_index=True)
    info.update({"carried_rows": int(len(carry)), "carried_ids_not_in_fresh": int(len(missing)), "fresh_rows_differ_from_carried": int(diff), "rows": int(len(out))})
    return ra._events_types(out), info


def lifecycle_revisions(prev_lc: pd.DataFrame, lc: pd.DataFrame, observed: pd.Timestamp) -> tuple[pd.DataFrame, dict, dict]:
    a = prev_lc.set_index("tradingItemId")
    b = lc.set_index("tradingItemId")
    common = a.index.intersection(b.index)
    rows = []
    for f in ("status", "lastTradeDate", "endReason", "firstTradeDate"):
        x, y = a.loc[common, f], b.loc[common, f]
        if str(x.dtype).startswith("datetime"):
            diff = ~((x.isna() & y.isna()) | (x == y))
        else:
            diff = x.astype(str) != y.astype(str)
        for tid in common[diff.to_numpy()]:
            rows.append({"tradingItemId": int(tid), "field": f, "old": str(a.at[tid, f]), "new": str(b.at[tid, f]), "observedAt": observed})
    lrev = pd.DataFrame(rows, columns=["tradingItemId", "field", "old", "new", "observedAt"])
    lrev["tradingItemId"] = lrev.tradingItemId.astype(np.int64)
    lrev["observedAt"] = pd.to_datetime(lrev.observedAt).astype("datetime64[ns]")
    trans = pd.crosstab(a.loc[common, "status"], b.loc[common, "status"])
    transitions = {f"{i}->{j}": int(trans.at[i, j]) for i in trans.index for j in trans.columns if trans.at[i, j] and i != j}
    return lrev, transitions, {"ids_added": int(len(b.index.difference(a.index))), "ids_removed": int(len(a.index.difference(b.index)))}


def finalize_master_rc2f(ctx, ovr: dict, alog: ra.ApplyLog, prev_events: pd.DataFrame | None, fresh_events: pd.DataFrame | None, ledger_path: Path | None,
                         fresh_lifecycle: bool = True) -> dict:
    """After T6 (U5), R-D2 rework order:
    (1) events of the day = previous events carried + delta ids (newly ended / lifecycle end changed / revived) from today's T6; transitions csv
    (2) delta ids re-classified with their final events row (same-day marker); active ids with a lagging miprice keep the ciq last close
    (3) lifecycle_dates rebuilt; rc2f lifecycle overrides re-applied on the fresh lifecycle / aux / dates / lineage; their events rows stay carried
    (4) firstTradeDate / firstPriceDate / firstTradeSource carried from the previous build unless the id's inputs changed; R3 first-date patch
    (5) lineage text columns of value-unchanged rows restored (idempotent evidence); aux rows appended; exclusions; revisions."""
    import master_aux as ma
    import master_lifecycle as ml
    import ledger_build as lb
    out, prev, args = ctx.out, ctx.prev, ctx.args
    res = {}
    lc_fresh = pd.read_parquet(out.master_file("lifecycle"))
    lc_fresh = lc_fresh[lc_fresh.status != "aux"] if "status" in lc_fresh.columns else lc_fresh
    lc_fresh = lc_fresh[lc_fresh.tradingItemId.isin(ctx.ids)].reset_index(drop=True)
    lc_fresh.to_parquet(out.t1_work / "lifecycle_pre_override.parquet", index=False)
    lc_prev_all = pd.read_parquet(prev.master_file("lifecycle")) if prev.master_file("lifecycle").exists() else None
    lc_prev = lc_prev_all[lc_prev_all.status != "aux"] if lc_prev_all is not None and "status" in lc_prev_all.columns else lc_prev_all
    lca = pd.read_parquet(out.master_file("lifecycle_aux")) if out.master_file("lifecycle_aux").exists() else pd.DataFrame(columns=ml.LIFECYCLE_COLS)
    if len(lca):
        lca = refresh_aux_lifecycle(lca, out.base_data, ctx.asof, ctx.observed, ctx.read_rows)
    ovr_lc = ovr.get("lifecycle", pd.DataFrame())
    override_ids = set(int(i) for i in ovr_lc.x_tid.dropna().astype(int)) if len(ovr_lc) else set()
    if fresh_events is None and prev_events is None:
        raise RuntimeError("finalize: no events file (neither T6 output nor previous build events)")
    # (0) ended ids with unchanged inputs keep the snapshot's end dates (marker-source transition guard)
    if lc_prev is not None and fresh_lifecycle:
        lc_fresh, ce_info = carry_end_dates(lc_fresh, lc_prev, out.t1_qa / "lifecycle_end_dates_carried.csv")
        res["end_dates_carried"] = ce_info
    # (1) events delta
    if fresh_events is not None and lc_prev is not None and fresh_lifecycle:
        d = events_delta_ids(lc_prev, lc_fresh, prev_events, fresh_events, ctx.prev_asof, ctx.asof, override_ids)
        delta = d["delta"]
        res["events_delta"] = {k: (sorted(v)[:50] if k == "delta" else len(v)) for k, v in d.items()}
        res["events_delta"]["delta_n"] = len(delta)
    else:
        delta = set()
        res["events_delta"] = {"delta_n": 0, "note": "T6 not run today or no previous lifecycle: events carried"}
    (out.t6 / "qa").mkdir(parents=True, exist_ok=True)
    ev, ev_info = events_delta_merge(fresh_events if prev_events is not None else fresh_events, prev_events, delta, out.t6 / "qa" / "events_transitions.csv")
    res["events_merge"] = ev_info
    cal_days = ctx.calendar.astype("datetime64[ns]")
    # (2) same-day marker for delta ids, mi lag rule
    frame_full = pd.read_parquet(out.t1_work / "lifecycle_full.parquet") if (out.t1_work / "lifecycle_full.parquet").exists() else None
    lc_fresh, n_recl = reclassify_delta(lc_fresh, frame_full, ev, delta, cal_days, ctx.asof)
    lc_fresh, n_milag = mi_lag_rule(lc_fresh, cal_days, ctx.asof, int(getattr(args, "mi_lag_tdays", 0)))
    lc_fresh, n_rowless = carry_last_dates_rowless(lc_fresh, lc_prev)
    res["reclassified_delta_ids"], res["mi_lag_ids"], res["rowless_ids_dates_carried"] = n_recl, n_milag, n_rowless
    # (3) dates + overrides
    ln = pd.read_parquet(out.master_file("lineage"))
    prev_ln = pd.read_parquet(prev.master_file("lineage")) if prev.master_file("lineage").exists() else None
    cal_df = pd.read_parquet(out.calendar)
    cal = calendar_from_frame(cal_df)
    ledger = pd.read_parquet(ledger_path) if ledger_path is not None and Path(ledger_path).exists() else None
    top500 = set()
    if prev.master_file("lifecycle_dates").exists():
        d0 = pd.read_parquet(prev.master_file("lifecycle_dates"), columns=["tradingItemId", "everTop500"])
        top500 = set(int(i) for i in d0.loc[d0.everTop500, "tradingItemId"])
    lc_all_pre = pd.concat([lc_fresh, lca[lc_fresh.columns]] if len(lca) else [lc_fresh], ignore_index=True)
    dates = ma.build_lifecycle_dates(lc_all_pre, ev, ledger[["tradingItemId", "exDate", "eventType", "payDate"]] if ledger is not None else None, top500)
    res["lifecycle_dates_rebuilt"] = int(len(dates))
    changed = set()
    if len(ovr_lc) and not fresh_lifecycle:
        for r in ovr_lc.itertuples(index=False):
            alog.set(r.overrideId, caseId=r.caseId, kind="lifecycle", action=r.treatment.strip(), status="carried", reason="lifecycle carried (U4 not run)", affected_rows=0,
                     target=str(r.x_tid), file="lifecycle")
        res["lifecycle_overrides"] = {"rows": int(len(ovr_lc)), "carried": True}
    elif len(ovr_lc):
        ids = override_ids | set(int(x) for x in ovr_lc.successorTradingItemId.map(ra.parse_int) if x is not None and pd.notna(x))
        closes = lb.RawCloses([out.base_data], ids)
        touched = set()
        if len(ovr.get("lineage", pd.DataFrame())):
            touched = {(int(float(a)), int(float(b))) for a, b in zip(ovr["lineage"].oldId, ovr["lineage"].newId) if str(a).strip() and str(b).strip()}
        r = ra.apply_lifecycle_overrides(ovr_lc, lc_fresh.set_index("tradingItemId"), lca.set_index("tradingItemId") if len(lca) else None, dates.set_index("tradingItemId"),
                                         ev, ev.copy(), ln, closes, cal, alog, top500, cal_days=cal_days, lineage_touched=touched)
        changed = r["changed"]
        lc_fresh = r["lifecycle"] if "lifecycle" in changed else lc_fresh
        if "lifecycle_aux" in changed and r["lifecycle_aux"] is not None:
            lca = r["lifecycle_aux"]
        if "lifecycle_dates" in changed:
            dates = r["lifecycle_dates"]
        if "lineage" in changed:
            ln = r["lineage"]
        if "events_reclass" in changed:
            # the carried events rows of the override ids already hold the rc2f patch (rc2f2); re-patched rows are taken only for override ids
            # in today's delta and for rows the override creates (id absent from the carried events)
            evp = r["events_reclass"].set_index("tradingItemId")
            evk = ev.set_index("tradingItemId")
            take = [t for t in evp.index if (int(t) in delta and int(t) in override_ids) or int(t) not in evk.index]
            if take:
                evk = pd.concat([evk.drop(index=[t for t in take if t in evk.index]), evp.loc[take]])
                ev = ra._events_types(evk.reset_index())
            res["events_rows_repatched"] = len(take)
        res["lifecycle_overrides"] = {"rows": int(len(ovr_lc)), "changed": sorted(changed)}
    # (4) first dates: carry unless inputs changed; R3 patch (aux predecessor / stitched)
    fd_info = {}
    if lc_prev is not None and fresh_lifecycle:
        lc_fresh, fd_info = carry_first_dates(lc_fresh, lc_prev, ln, delta, override_ids, ctx.new_ids)
    lc_fresh = lc_fresh[ml.LIFECYCLE_COLS].reset_index(drop=True)
    if len(lca):
        lca = lca[ml.LIFECYCLE_COLS].reset_index(drop=True)
    lc_fresh, patch_rows, lca_p = ml.lineage_v2_first_date_patch(ln, lc_fresh, lca if len(lca) else None, None, cal_days)
    if lca_p is not None:
        lca = lca_p
    fd_info["r3_first_date_patch_rows"] = int(len(patch_rows))
    if len(patch_rows):
        patch_rows.to_csv(out.t1_qa / "lifecycle_first_date_patch_daily.csv", index=False)
    res["first_dates"] = fd_info
    # (5) chain gaps with the final lifecycle (carried first dates), then lineage text idempotence
    lc_for_gap = pd.concat([lc_fresh, lca[lc_fresh.columns]] if len(lca) else [lc_fresh], ignore_index=True)
    ln = recompute_chain_gaps(ln, lc_for_gap, cal_days, prev_ln=prev_ln, lc_prev=lc_prev_all)
    ln, n_text = restore_unchanged_lineage_text(prev_ln, ln)
    res["lineage_text_restored"] = n_text
    # ---- write
    lc_fresh = lc_fresh[ml.LIFECYCLE_COLS].sort_values("tradingItemId").reset_index(drop=True)
    lc_fresh["tradingItemStatusId"] = lc_fresh.tradingItemStatusId.astype("Int64")
    for c in ("firstTradeDate", "firstPriceDate", "ciqLastCloseDate", "miLastCloseDate", "securityEndDate", "eventEndDate", "lastTradeDate", "observedAt"):
        lc_fresh[c] = pd.to_datetime(lc_fresh[c]).astype("datetime64[ns]")
    lc_instr_path = out.t1_work / "lifecycle_instruments.parquet"
    write_like(lc_fresh, prev.master_file("lifecycle"), lc_instr_path)
    if len(lca):
        lca = lca[ml.LIFECYCLE_COLS].sort_values("tradingItemId").reset_index(drop=True)
        lca["tradingItemStatusId"] = lca.tradingItemStatusId.astype("Int64")
        for c in ("firstTradeDate", "firstPriceDate", "ciqLastCloseDate", "miLastCloseDate", "securityEndDate", "eventEndDate", "lastTradeDate", "observedAt"):
            lca[c] = pd.to_datetime(lca[c]).astype("datetime64[ns]")
        if set(lca.tradingItemId) & set(lc_fresh.tradingItemId):
            raise RuntimeError("aux lifecycle ids overlap the instruments lifecycle")
        write_like(lca, prev.master_file("lifecycle_aux") if prev.master_file("lifecycle_aux").exists() else prev.master_file("lifecycle"), out.master_file("lifecycle_aux"))
        lc_full = pd.concat([lc_fresh, lca], ignore_index=True).sort_values("tradingItemId").reset_index(drop=True)
    else:
        lc_full = lc_fresh
    write_like(lc_full, prev.master_file("lifecycle"), out.master_file("lifecycle"))
    write_like(dates, prev.master_file("lifecycle_dates") if prev.master_file("lifecycle_dates").exists() else None, out.master_file("lifecycle_dates"))
    ln = ln.sort_values(["oldId", "newId"]).reset_index(drop=True)
    ra._write_parquet_like(ln, prev.master_file("lineage"), out.master_file("lineage"), extra={"linkVerified": pa.bool_()})
    if "reviewFlag" in ev.columns:
        ev["reviewFlag"] = dedupe_tags(ev.reviewFlag)               # D3 (1): rc2f / rule tags are a set
    write_events(ev, prev.events if prev.events.exists() else None, out.events)
    # tradable_exclusions: fresh step output + aux ids (reason aux_instrument), deduplicated
    ex_p = out.master_file("tradable_exclusions")
    ex = pd.read_parquet(ex_p) if ex_p.exists() else pd.DataFrame(columns=["tradingItemId", "fromDate", "toDate", "reason", "preferredItemId"])
    ex = ex[ex.reason != "aux_instrument"]
    if len(lca):
        add = pd.DataFrame({"tradingItemId": lca.tradingItemId.astype("int64"), "fromDate": pd.Timestamp("1900-01-01"), "toDate": pd.Timestamp("2100-01-01"),
                            "reason": "aux_instrument", "preferredItemId": pd.array([pd.NA] * len(lca), dtype="Int64")})
        ex = pd.concat([ex, add], ignore_index=True)
    ex["preferredItemId"] = ex["preferredItemId"].astype("Int64")
    ex["tradingItemId"] = ex.tradingItemId.astype("int64")
    for c in ("fromDate", "toDate"):
        ex[c] = pd.to_datetime(ex[c]).astype("datetime64[ns]")
    ex_instr = ex[ex.reason != "aux_instrument"].reset_index(drop=True)
    ex_instr.to_parquet(out.t1_work / "tradable_exclusions_instruments.parquet", index=False)
    ex.reset_index(drop=True).to_parquet(ex_p, index=False)
    # revisions vs the previous build (post-override lifecycle on both sides)
    if lc_prev_all is not None:
        lrev, trans, sets = lifecycle_revisions(lc_prev_all, lc_full, ctx.observed)
        lrev.to_parquet(out.revisions_dir(ctx.observed) / "lifecycle_revisions.parquet", index=False)
        res["lifecycle_revisions"] = lrev.field.value_counts().to_dict()
        res["status_transitions"] = trans
        res.update(sets)
    res["lifecycle"] = {"rows": int(len(lc_full)), "instruments": int(len(lc_fresh)), "aux": int(len(lca)), "status_counts": lc_full.status.value_counts().to_dict()}
    res["lineage"] = {"rows": int(len(ln)), "chainable_true": int(ln.chainable.sum()), "linkVerified_true": int(ln.linkVerified.sum()) if "linkVerified" in ln.columns else 0}
    res["events"] = {"rows": int(len(ev)), "override_rows": int(ev.overrideSource.fillna("").astype(str).ne("").sum())}
    return res


def finalize_scope(ctx, ledger_path: Path, alog: ra.ApplyLog) -> dict:
    """After the ledger of the day exists: settlementReturnScope labels (R-X4 d) on the final events with the day's ledger, payDate of
    lifecycle_dates rows sourced from the ledger refreshed (override-sourced rows untouched); events / lifecycle_dates rewritten."""
    out, prev = ctx.out, ctx.prev
    res = {}
    if ledger_path is None or not Path(ledger_path).exists():
        return {"skipped": "no ledger"}
    ledger = pd.read_parquet(ledger_path)
    ev = pd.read_parquet(out.events)
    if "settlementReturnScope" not in ev.columns:
        ev["settlementReturnScope"] = "full"
    lc_all = pd.read_parquet(out.master_file("lifecycle"))
    cal_df = pd.read_parquet(out.calendar)
    cal = calendar_from_frame(cal_df)
    ev, warn_legs, n_scope = ra.label_settlement_scope(ev, ledger, lc_all, cal, alog)
    if "reviewFlag" in ev.columns:
        ev["reviewFlag"] = dedupe_tags(ev.reviewFlag)
    (out.t6 / "qa").mkdir(parents=True, exist_ok=True)
    warn_legs.to_csv(out.t6 / "qa" / "ca_ledger_legs_after_settlement_bar.csv", index=False)
    n_carried = 0
    for tid in ev.loc[ev.settlementReturnScope.astype(str) == "events_legs_only", "tradingItemId"]:
        oid = f"RULE-X4-scope:{int(tid)}"
        if oid not in alog.rows:
            alog.set(oid, kind="rule", action="settlementReturnScope", status="carried", affected_rows=0, target=str(int(tid)), file="events", reason="events_legs_only label carried")
            n_carried += 1
    res["settlement_scope"] = {"events_rows_relabelled": int(n_scope), "labels_carried": n_carried, "value_legs_after_settlement_bar": int(len(warn_legs))}
    write_events(ev, prev.events if prev.events.exists() else None, out.events)
    dp = out.master_file("lifecycle_dates")
    if dp.exists():
        dates = pd.read_parquet(dp)
        lg = ledger[ledger.eventType.isin(["merger_cash", "merger_stock"]) & ledger.payDate.notna()].sort_values("exDate").drop_duplicates("tradingItemId", keep="last").set_index("tradingItemId")
        src = dates.payDateSource.fillna("").astype(str)
        refresh = src.isin(["", "ca_ledger.payDate"]) & dates.tradingItemId.isin(lg.index)
        n_pay = 0
        if refresh.any():
            new_pay = pd.to_datetime(dates.loc[refresh, "tradingItemId"].map(lg.payDate)).to_numpy().astype("datetime64[ns]")
            old_pay = dates.loc[refresh, "payDate"].to_numpy().astype("datetime64[ns]")
            n_pay = int((~((np.isnat(old_pay) & np.isnat(new_pay)) | (old_pay == new_pay))).sum())
            dates.loc[refresh, "payDate"] = new_pay
            dates.loc[refresh, "payDateSource"] = "ca_ledger.payDate"
            write_like(dates, prev.master_file("lifecycle_dates") if prev.master_file("lifecycle_dates").exists() else None, dp)
        res["lifecycle_dates_payDate_refreshed"] = n_pay
    return res


def refresh_aux_lifecycle(lca: pd.DataFrame, base_path: Path, asof: pd.Timestamp, observed: pd.Timestamp, read_rows) -> pd.DataFrame:
    """aux ids: ciqLastCloseDate / lastTradeDate = last close in the merged base_data (their rows were part of the window fetch);
    endReason 'active' when quoted within 14 days of asOf and no end marker, else unchanged; status stays 'aux'."""
    lca = lca.copy()
    ids = np.array(sorted(set(lca.tradingItemId.astype(int))), dtype=np.int64)
    rows = read_rows(base_path, ["tradingItemId", "date", "close"], ids=ids)
    rows = rows[np.isfinite(rows.close.to_numpy(dtype=np.float64))]
    g = rows.groupby("tradingItemId").date.agg(["min", "max"])
    k = lca.set_index("tradingItemId")
    for tid in g.index:
        if tid not in k.index:
            continue
        last = pd.Timestamp(g.at[tid, "max"])
        first = pd.Timestamp(g.at[tid, "min"])
        if str(k.at[tid, "lastTradeSource"]) in ("ciq", ""):
            k.at[tid, "ciqLastCloseDate"] = last
            k.at[tid, "lastTradeDate"] = last
            k.at[tid, "lastTradeSource"] = "ciq"
        if pd.isna(k.at[tid, "firstTradeDate"]):
            k.at[tid, "firstTradeDate"] = first
            k.at[tid, "firstPriceDate"] = first
            k.at[tid, "firstTradeSource"] = "ciq"
        marker = pd.notna(k.at[tid, "securityEndDate"]) or (pd.notna(k.at[tid, "tradingItemStatusId"]) and int(k.at[tid, "tradingItemStatusId"]) in (2, 4, 5, 8, 11))
        if last >= pd.Timestamp(asof) - pd.Timedelta(days=14) and not marker:
            k.at[tid, "endReason"] = "active"
    k["observedAt"] = pd.Timestamp(observed)
    k["status"] = "aux"
    return k.reset_index()


def write_like(df: pd.DataFrame, src: Path | None, dst: Path) -> None:
    """Write df with the arrow schema of src (column order/dtypes of the previous build's file) when src exists, else plain."""
    if src is not None and Path(src).exists():
        try:
            ra._write_parquet_like(df, Path(src), Path(dst))
            return
        except Exception as exc:  # noqa: BLE001  (schema drift: fall back to a plain write, logged)
            log.warning("write_like %s: schema of %s not reusable (%s); plain write", dst, src, exc)
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(dst, index=False)


def write_events(ev: pd.DataFrame, src: Path | None, dst: Path) -> None:
    ev = ra._events_types(ev)
    write_like(ev, src, dst)


# ======================================================================================
# aux additions (new ids only)
# ======================================================================================
def reapply_aux_additions(ctx, ovr_aux: pd.DataFrame, alog: ra.ApplyLog, prev_ledger: pd.DataFrame | None) -> dict:
    """Ids of aux_additions.csv already in the build (instruments or aux) -> carried. New ids -> rc2f_apply.apply_aux_additions on a
    temporary R3 layout built from the previous build (identity/prices/lifecycle/factors via xfeed, read-only); the new price rows are
    handed to the U2/U3 merge as full-scope rows, the factors appended, aux_instruments / lifecycle_aux replaced, cash dividend rows
    written to rc2f/for_R1_aux_dividends.csv (ledger builder input)."""
    out, prev = ctx.out, ctx.prev
    res = {"rows": int(len(ovr_aux)) if ovr_aux is not None else 0, "carried": 0, "new": 0}
    if ovr_aux is None or not len(ovr_aux):
        return res
    have = set(int(i) for i in ctx.ids) | set(int(i) for i in ctx.aux_ids)
    new_rows = []
    for r in ovr_aux.itertuples(index=False):
        tid = int(r.x_tid) if r.x_tid is not None else None
        if tid is None:
            alog.set(r.overrideId, kind="aux", action="add", status="rejected", reason="tradingItemId empty", affected_rows=0, file="aux")
        elif tid in have:
            alog.set(r.overrideId, kind="aux", action="add", status="carried", reason="already an aux/instrument id of the build", affected_rows=0, target=str(tid), file="aux")
            res["carried"] += 1
        else:
            new_rows.append(r)
    if not new_rows:
        return res
    res["new"] = len(new_rows)
    df = ovr_aux[ovr_aux.overrideId.isin([r.overrideId for r in new_rows])]
    r3s = out.rc2f / "aux_r3_prev"
    out_r3 = out.rc2f / "aux_r3_new"
    work = out.rc2f / "aux_work"
    for d in (r3s / "master", r3s / "factors_aux", out_r3 / "master", out_r3 / "factors_aux", work):
        d.mkdir(parents=True, exist_ok=True)
    try:
        aux_prev = pd.read_parquet(prev.master_file("aux_instruments"))
        aux_prev.to_parquet(r3s / "master" / "aux_instruments.parquet", index=False)
        lca = pd.read_parquet(prev.master_file("lifecycle_aux"))
        lca.to_parquet(r3s / "master" / "lifecycle_aux.parquet", index=False)
        aux_ids = np.array(sorted(set(aux_prev.tradingItemId.astype(int))), dtype=np.int64)
        rows = ctx.read_rows(prev.base_data, None, ids=aux_ids)
        pq.write_table(pa.Table.from_pandas(rows, schema=pq.ParquetFile(prev.base_data).schema_arrow.remove_metadata(), preserve_index=False), r3s / "base_data_aux.parquet", compression="zstd")
        for name in ("split", "dividend"):
            f = pd.read_parquet(prev.t2 / "factors" / f"{name}.parquet")
            f[f.tradingItemId.isin(aux_ids)].reset_index(drop=True).to_parquet(r3s / "factors_aux" / f"{name}.parquet", index=False)
        pd.DataFrame(columns=["tradingItemId", "exDate", "eventType", "legType", "cashPerShare", "cashCurrency", "source", "evidence", "status", "isEstimate", "notes"]).to_csv(r3s / "for_R1_events.csv", index=False)
        ref = LightRef(have, prev_ledger.tradingItemId.unique() if prev_ledger is not None else [], prev_ledger)
        info = ra.apply_aux_additions(df, r3s, out_r3, alog, ref, str(ctx.asof.date()) if ctx.asof is not None else None, work, prev_ledger)
        res["apply"] = {k: v for k, v in info.items() if not isinstance(v, (pd.DataFrame,))}
        # hand-over of the results to the build
        px = out_r3 / "base_data_aux.parquet"
        new_ids = set()
        if px.exists() and not px.is_symlink():
            t = pq.read_table(px)
            prev_ids = set(int(i) for i in aux_ids)
            t = t.filter(pc.invert(pc.is_in(t["tradingItemId"], value_set=pa.array(sorted(prev_ids), pa.int64()))))
            new_ids = set(t["tradingItemId"].unique().to_pylist())
            if t.num_rows:
                side = pa.table({"tradingItemId": t["tradingItemId"], "date": t["date"], "adjustmentfactor": pa.nulls(t.num_rows, pa.float64()), "ciqClose": pa.nulls(t.num_rows, pa.float64())})
                ctx.pend.add_full(t.cast(pq.ParquetFile(out.base_data if out.base_data.exists() else prev.base_data).schema_arrow.remove_metadata()), side, sorted(new_ids))
        for name in ("split", "dividend"):
            f_new = out_r3 / "factors_aux" / f"{name}.parquet"
            if f_new.exists() and not f_new.is_symlink():
                ctx.aux_factor_rows[name] = pd.read_parquet(f_new)
                ctx.aux_factor_rows[name] = ctx.aux_factor_rows[name][ctx.aux_factor_rows[name].tradingItemId.isin(sorted(new_ids))]
        for name in ("aux_instruments", "lifecycle_aux"):
            p = out_r3 / "master" / f"{name}.parquet"
            if p.exists() and not p.is_symlink():
                shutil.copy2(p, out.master_file(name))
        ev_csv = out_r3 / "for_R1_events.csv"
        if ev_csv.exists() and not ev_csv.is_symlink():
            shutil.copy2(ev_csv, out.rc2f / "for_R1_aux_dividends.csv")
        ctx.aux_ids = np.array(sorted(set(int(i) for i in ctx.aux_ids) | new_ids), dtype=np.int64)
        res["new_ids"] = sorted(new_ids)
    except Exception as exc:  # noqa: BLE001  (never silent: every new id becomes a rejected override -> G-OVR fails)
        for r in new_rows:
            alog.set(r.overrideId, kind="aux", action="add", status="rejected", reason=f"aux addition failed: {type(exc).__name__}: {str(exc)[:200]}", affected_rows=0, target=str(r.x_tid), file="aux")
        res["error"] = f"{type(exc).__name__}: {exc}"
    return res


# ======================================================================================
# ledger incremental (D1 CLI)
# ======================================================================================
def ledger_cli_supports(script: Path, flag: str) -> bool:
    try:
        txt = script.read_text(errors="ignore")
    except OSError:
        return False
    return f'"{flag}"' in txt or f"'{flag}'" in txt or f'f"--{{name}}"' in txt and flag.lstrip("-") in ("calendar", "events", "instruments", "current", "membership")


def run_ledger_incremental(ctx, run_cmd, published_view: Path, overrides_glob: str) -> dict:
    """ledger_build.py --incremental --prev <previous build root (published layout)> --asof <T-1> --out <T6 stage> [--overrides-glob ...]
    (+ --base-data / --lineage / --lifecycle of the new build when the CLI accepts them). Output contract: <stage>/events/ca_ledger.parquet
    + ca_ledger_build_info.json (asOf = build asOf) + repairs/copies/QA csv + ca_ledger_revisions.parquet (added/changed/removed rows of the day)."""
    out, args = ctx.out, ctx.args
    script = HERE / "ledger_build.py"
    if not (HERE / "ledger_incremental.py").exists() or not ledger_cli_supports(HERE / "ledger_incremental.py", "--incremental"):
        return {"skipped": "ledger_build.py has no --incremental (D1 not merged): ledger resolved by --ledger / carry rules"}
    (out.t6 / "events").mkdir(parents=True, exist_ok=True)
    prev_stage = ctx.prev.t6 if (ctx.prev.layout == "stage" and (ctx.prev.t6 / "events" / "ca_ledger.parquet").exists()) else published_view
    if getattr(args, "ledger_prev", None):
        prev_stage = Path(args.ledger_prev)              # bootstrap from a published build: the rc2f R1 stage carries the apply hand-over (split moves, estimated prices)
    cmd = [sys.executable, str(script), "--incremental", "--prev", str(prev_stage), "--asof", f"{ctx.asof:%Y-%m-%d}", "--out", str(out.t6)]
    if ledger_cli_supports(HERE / "ledger_incremental.py", "--build"):
        cmd += ["--build", str(out.root)]
    if overrides_glob:
        cmd += ["--overrides-glob", overrides_glob]
    if getattr(args, "ledger_extra_args", ""):
        cmd += [a for a in args.ledger_extra_args.split() if a]
    r = run_cmd(cmd, out.cmd_log, timeout_s=args.t5_timeout)
    res = {"cmd": r["cmd"], "rc": r["rc"], "seconds": r["seconds"]}
    led = out.t6 / "events" / "ca_ledger.parquet"
    side = led.with_name("ca_ledger_build_info.json")
    res["ledger_written"] = led.exists()
    if r["rc"] == 0 and led.exists() and side.exists():
        info = json.loads(side.read_text())
        res["asOf"] = info.get("asOf")
        res["rows"] = info.get("file", {}).get("rows")
        res["sha256"] = info.get("file", {}).get("sha256")
        res["csv_overrides"] = info.get("csv_overrides")
        res["incremental"] = info.get("incremental")
    # revisions of the day -> archive
    revs = sorted(out.t6.glob("events/ca_ledger_revisions/date=*/revisions.parquet")) + sorted(out.t6.glob("**/ca_ledger_revisions*.parquet"))
    if revs:
        dst = out.revisions_dir(ctx.observed) / "ca_ledger_revisions.parquet"
        shutil.copy2(revs[-1], dst)
        res["revisions_file"] = str(dst)
        try:
            res["revisions_rows"] = int(pq.ParquetFile(dst).metadata.num_rows)
        except Exception:  # noqa: BLE001
            pass
    return res


def ledger_csv_override_statuses(ledger_dir: Path, alog: ra.ApplyLog, ovr_ledger: pd.DataFrame | None) -> int:
    """Ledger overrides are applied by the ledger builder (rc2f rule): its ca_ledger_csv_overrides.csv feeds the daily apply log."""
    n = 0
    p = Path(ledger_dir) / "ca_ledger_csv_overrides.csv"
    if p.exists():
        for r in pd.read_csv(p, dtype=str, keep_default_na=False).itertuples(index=False):
            alog.set(r.overrideId, caseId=getattr(r, "caseId", ""), kind="ledger", action=getattr(r, "action", ""), status=r.status, reason=getattr(r, "reason", ""),
                     affected_rows=int(float(getattr(r, "affected_rows", 0) or 0)), target=getattr(r, "target", ""), file="ledger", eventIds=getattr(r, "eventIds", ""))
            n += 1
    if ovr_ledger is not None and len(ovr_ledger):
        for r in ovr_ledger.itertuples(index=False):
            if r.overrideId not in alog.rows:
                alog.set(r.overrideId, caseId=r.caseId, kind="ledger", action=str(getattr(r, "action", "")), status="carried" if not p.exists() else "rejected",
                         reason="ledger carried from the previous build (builder not run today)" if not p.exists() else "not_reported_by_ledger_build",
                         affected_rows=0, target=str(getattr(r, "eventId", "") or ""), file="ledger")
    return n


# ======================================================================================
# returns v2 QA (stage) + attribution of the changed cells vs the previous build
# ======================================================================================
def run_returns_qa(ctx, run_cmd, vendor_split: Path, rc2f_stage: Path, rc1_returns: Path | None) -> dict:
    """returns_v2_qa.py on the stage (build root = a published-like view of the new build). G-VENDOR needs the rc1 vendor-factor
    returns of the same base_data: prices_returns legacy computed into T2_work (rc1_returns) when --returns-qa-vendor is set,
    else the gate reports G-VENDOR as skipped (G-CASH/SPLIT/NAN/LINK/ORACLE still run)."""
    out, args = ctx.out, ctx.args
    view = build_stage_view(ctx)
    qa_out = out.t2_qa / "returns_v2"
    cmd = [sys.executable, str(HERE / "returns_v2_qa.py"), "--build", str(view), "--base-data", str(out.base_data), "--v2", str(out.returns),
           "--ledger", str(out.ledger), "--lineage", str(out.master_file("lineage")), "--lifecycle", str(out.master_file("lifecycle")), "--out", str(qa_out),
           "--vendor-factors", str(vendor_split), "--rc2f-stage", str(rc2f_stage), "--threads", str(args.threads)]
    if rc1_returns is not None and Path(rc1_returns).exists():
        cmd += ["--rc1", str(rc1_returns)]
    r = run_cmd(cmd, out.cmd_log, timeout_s=args.t5_timeout)
    res = {"cmd": r["cmd"], "rc": r["rc"], "seconds": r["seconds"], "out": str(qa_out)}
    gp = qa_out / "gates.json"
    if gp.exists():
        G = json.loads(gp.read_text())
        res["gates"] = {k: (v.get("pass") if isinstance(v, dict) else v) for k, v in G.items() if k.startswith("G-")}
        res["retFlag"] = G.get("retFlag")
        res["G-VENDOR"] = {"diff_cells": G.get("G-VENDOR", {}).get("diff_cells"), "unexplained": G.get("G-VENDOR", {}).get("unexplained"), "classes": G.get("G-VENDOR", {}).get("classes")}
        res["G-SPLIT"] = {k: G.get("G-SPLIT", {}).get(k) for k in ("rows_checked", "fail", "ledger_vs_vendor_mismatch", "csv_override_rows")}
        res["G-LINK"] = {k: G.get("G-LINK", {}).get(k) for k in ("links_recomputed", "v2_linked_rows", "set_equal", "value_or_flag_mismatch")}
        res["G-NAN"] = {k: G.get("G-NAN", {}).get(k) for k in ("nan_rows", "nan_not_first_row", "ids_nan_first_row_but_link_resolvable")}
        res["G-ORACLE"] = {"pass": G.get("G-ORACLE", {}).get("pass"), "n": G.get("G-ORACLE", {}).get("n", G.get("G-ORACLE", {}).get("cases"))}
        res["pass"] = bool(G.get("pass")) and r["rc"] == 0
        if rc1_returns is None:
            # no rc1 reference: G-VENDOR compared v2 against itself (build view has no rc1 file) -> mark skipped, judge the others
            res["pass"] = r["rc"] == 0 and all(G[g]["pass"] for g in ("G-CASH", "G-SPLIT", "G-ORACLE", "G-NAN", "G-LINK"))
            res["gates"]["G-VENDOR"] = "skipped(no rc1 reference)"
    else:
        res["pass"] = False
        res["error"] = "gates.json not written"
    return res


def _masked_keys(ret_path: Path, lifecycle: pd.DataFrame | None, trading_days: np.ndarray | None) -> dict:
    """rc2f_recount.load_returns + the publish masks (rows after lastTradeDate dropped, off-calendar dates dropped) so a stage
    (unmasked) returns file compares like for like with a published one."""
    R = rr.load_returns(ret_path)
    keep = np.ones(len(R["key"]), dtype=bool)
    ids = rr.key_ids(R["key"])
    days = rr.key_days(R["key"])
    if lifecycle is not None:
        last = lifecycle.set_index("tradingItemId").lastTradeDate
        ltd = last.reindex(ids).to_numpy().astype("datetime64[D]").astype(np.int64)
        has = ~pd.isna(last.reindex(ids).to_numpy())
        keep &= ~has | (days <= ltd)
    if trading_days is not None and len(trading_days):
        td = np.asarray(trading_days).astype("datetime64[D]").astype(np.int64)
        keep &= np.isin(days, td)
    return {"key": R["key"][keep], "ret": R["ret"][keep], "flag": R["flag"][keep]}


def attribute_returns_changes(ctx, ledger_prev: Path | None, ledger_new: Path | None, ovr_cmp: dict, alog: ra.ApplyLog, lineage_prev: pd.DataFrame | None,
                              lineage_new: pd.DataFrame, lifecycle_rev: pd.DataFrame | None, price_rev: pd.DataFrame | None, factor_rev: pd.DataFrame | None,
                              ledger_revisions: pd.DataFrame | None = None) -> dict:
    """G-RET-ATTR: every changed returns cell (value / flag / added / removed; rc2f_recount.returns_diff) vs the previous build is
    attributed to one of (R-D2 P1-5 narrow windows): new_date (added cell dated after the previous asOf), price_revision (a revised /
    late / removed row and the next observation of that id - the only way a re-fetched window changes a return), factor_revision
    (changed split boundary +- 10 days), ledger_revision (changed ledger row: exDate .. first observation + 10 days, keeper/stitched ids,
    linked new id), override_status (override whose status changed / new override: the id), lineage (link rows and +-45 days around a
    changed pair), lifecycle (lastTradeDate moved: the rows between the old and the new date; firstTradeDate moved: around the first rows;
    the daily advance of an active name is not a change), new_id, aux_new, stale_cut. Unattributed cells -> FAIL. The same pass runs the
    value-sanity gate G-RET-OUTLIER (returns_outliers)."""
    out, prev = ctx.out, ctx.prev
    t0 = time.time()
    res = {"prev_returns": str(prev.returns), "new_returns": str(out.returns)}
    if not prev.returns.exists() or not out.returns.exists():
        return {**res, "pass": True, "note": "no previous returns to compare"}
    lc_new_full = pd.read_parquet(out.master_file("lifecycle"), columns=["tradingItemId", "firstTradeDate", "lastTradeDate"])
    lc_new = lc_new_full[["tradingItemId", "lastTradeDate"]]
    cal = pd.read_parquet(out.calendar)
    tdays = cal.loc[cal.isTradingDay, "date"].to_numpy().astype("datetime64[ns]")
    prev_lc = pd.read_parquet(prev.master_file("lifecycle"), columns=["tradingItemId", "lastTradeDate"]) if prev.master_file("lifecycle").exists() else None
    if prev.layout == "published":
        old = _masked_keys(prev.returns, None, None)                     # already masked by publish (previous lifecycle, previous calendar)
        new = _masked_keys(out.returns, lc_new, tdays)
    else:
        old = _masked_keys(prev.returns, prev_lc, None)
        new = _masked_keys(out.returns, lc_new, None)
    diff = rr.returns_diff(old, new)
    obs = rr.ObsDates(old, new)
    res["cells_changed"] = int(len(diff))
    res["by_kind"] = diff.kind.value_counts().to_dict() if len(diff) else {}
    res["outliers"] = returns_outliers(old, new, ctx.prev_asof, max_abs=float(getattr(ctx.args, "ret_abs_max", 50.0)), new_ids=list(ctx.new_ids) + list(getattr(ctx, "aux_new_ids", [])))
    if not len(diff):
        res.update({"pass": True, "unattributed": 0, "seconds": round(time.time() - t0, 1)})
        return res
    sc = rr.Scope()
    asof_day = int(np.datetime64(ctx.asof, "D").astype(np.int64)) if ctx.asof is not None else None
    prev_asof_day = int(np.datetime64(pd.Timestamp(ctx.prev_asof), "D").astype(np.int64))
    # price revisions: the revised row and the next observation
    if price_rev is not None and len(price_rev):
        for tid, d in price_rev[["tradingItemId", "date"]].drop_duplicates().itertuples(index=False):
            day = int(np.datetime64(pd.Timestamp(d), "D").astype(np.int64))
            nxt = obs.first_on_or_after(int(tid), day + 1)
            sc.add_window(int(tid), day, nxt if nxt is not None else day + 7, "price_revision", "revised_row+next_obs")
    # factor revisions (split): boundary +- tol
    if factor_rev is not None and len(factor_rev):
        fr = factor_rev[factor_rev.table.astype(str).str.startswith("split")]
        for r in fr.itertuples(index=False):
            day = int(np.datetime64(pd.Timestamp(r.validFrom), "D").astype(np.int64))
            sc.add_window(int(r.tradingItemId), day - FACTOR_TOL_DAYS, day + FACTOR_TOL_DAYS, "factor_revision", f"split {r.kind}")
            for c in ("validTo_old", "validTo_new"):
                v = getattr(r, c, None)
                if v is not None and pd.notna(v):
                    dd = int(np.datetime64(pd.Timestamp(v), "D").astype(np.int64))
                    sc.add_window(int(r.tradingItemId), dd - FACTOR_TOL_DAYS, dd + FACTOR_TOL_DAYS, "factor_revision", f"split {r.kind} validTo")
    # lineage-linked successors: a changed ledger row / lifecycle end of an old id moves the linked first row of its rename/relist new id
    succ = {}
    for r in lineage_new[lineage_new.relation.isin(["rename", "relist"])].itertuples(index=False):
        succ.setdefault(int(r.oldId), set()).add(int(r.newId))
    lc_first = lc_new_full.set_index("tradingItemId").firstTradeDate if "firstTradeDate" in lc_new_full.columns else pd.Series(dtype="datetime64[ns]")

    def link_windows(old_id: int, oid: str, how: str):
        for nid in succ.get(int(old_id), ()):
            fd = obs.first_day(nid)
            ft = lc_first.get(nid, pd.NaT)
            for center in ([fd] if fd is not None else []) + ([int(np.datetime64(pd.Timestamp(ft), "D").astype(np.int64))] if pd.notna(ft) else []):
                sc.add_window(nid, center - LINEAGE_TOL_DAYS, center + LINEAGE_TOL_DAYS, oid, how + f" (link row of {nid} from {old_id})")
    # ledger revisions: changed rows (diff prev vs new ledger, or the builder's revisions file)
    n_ledger_rows = 0
    if ledger_prev is not None and ledger_new is not None and Path(ledger_prev).exists() and Path(ledger_new).exists():
        cols = ["eventId", "tradingItemId", "exDate", "eventType", "legType", "childTradingItemId", "cashPerShare", "parentFactor", "childRatio", "childValueSource",
                "childValuePerOldShare", "status", "isEstimate", "source", "payDate", "repairOf", "keeperEventId", "notes"]
        lo_ = pd.read_parquet(ledger_prev, columns=cols)
        ln_ = pd.read_parquet(ledger_new, columns=cols)
        ld = rr.ledger_diff(lo_, ln_)
        n_ledger_rows = int(len(ld))
        ld.to_csv(out.t2_qa / "returns_attr_ledger_diff.csv", index=False)
        res["ledger_rows_changed"] = n_ledger_rows
        res["ledger_rows_by_change"] = ld.change.value_counts().to_dict() if len(ld) else {}
        keeper_ids = {}
        if len(ln_):
            kk = ln_[ln_.keeperEventId.fillna("").astype(str) != ""]
            for r in kk.itertuples(index=False):
                keeper_ids.setdefault(str(r.keeperEventId), set()).add(int(r.tradingItemId))
        for r in ld.itertuples(index=False):
            day = int(np.datetime64(pd.Timestamp(r.exDate), "D").astype(np.int64))
            for tid in {int(r.tradingItemId)} | ({int(r.childTradingItemId)} if int(r.childTradingItemId) > 0 else set()):
                nxt = obs.first_on_or_after(tid, day)
                sc.add_window(tid, day - 1, (nxt if nxt is not None else day) + LEDGER_TOL_DAYS, "ledger_revision", f"{r.change}:{r.eventType}/{r.legType}")
            link_windows(int(r.tradingItemId), "ledger_revision", f"{r.change}:{r.eventType}/{r.legType}")
            for eid in (r.eventIdNew, r.eventIdOld):
                for tid in keeper_ids.get(str(eid), set()):
                    nxt = obs.first_on_or_after(tid, day)
                    sc.add_window(tid, day - 1, (nxt if nxt is not None else day) + LEDGER_TOL_DAYS, "ledger_revision", f"stitched_copy_of:{r.change}")
    if ledger_revisions is not None and len(ledger_revisions) and "tradingItemId" in ledger_revisions.columns and "exDate" in ledger_revisions.columns:
        for r in ledger_revisions[["tradingItemId", "exDate"]].drop_duplicates().itertuples(index=False):
            day = int(np.datetime64(pd.Timestamp(r.exDate), "D").astype(np.int64))
            nxt = obs.first_on_or_after(int(r.tradingItemId), day)
            sc.add_window(int(r.tradingItemId), day - 1, (nxt if nxt is not None else day) + LEDGER_TOL_DAYS, "ledger_revision", "builder_revisions")
    # override status changes / new overrides: the whole id
    lg = alog.frame()
    changed_oids = set(x["overrideId"] for x in ovr_cmp.get("status_changed", []) + ovr_cmp.get("regressions", []) + ovr_cmp.get("rejected", [])) | set(ovr_cmp.get("new_ids", []))
    if changed_oids and len(lg):
        for r in lg[lg.overrideId.isin(changed_oids)].itertuples(index=False):
            tgt = str(r.target)
            for tok in tgt.replace("->", " ").replace("@", " ").split():
                if tok.isdigit():
                    sc.add_window(int(tok), -(1 << 40), 1 << 40, "override_status", f"{r.overrideId}:{r.status}")
    # lineage changes: pairs added/removed/changed (ratio, chainable, effectiveDate, relation)
    if lineage_prev is not None and len(lineage_prev):
        a = lineage_prev.set_index(["oldId", "newId"])
        b = lineage_new.set_index(["oldId", "newId"])
        keys = set(a.index) ^ set(b.index)
        for k in a.index.intersection(b.index):
            ra_, rb_ = a.loc[k], b.loc[k]
            same_ratio = (pd.isna(ra_.ratio) and pd.isna(rb_.ratio)) or (pd.notna(ra_.ratio) and pd.notna(rb_.ratio) and abs(float(ra_.ratio) - float(rb_.ratio)) <= 1e-12)
            if not same_ratio or bool(ra_.get("chainable", False)) != bool(rb_.get("chainable", False)) or str(ra_.relation) != str(rb_.relation) \
                    or not ((pd.isna(ra_.effectiveDate) and pd.isna(rb_.effectiveDate)) or pd.Timestamp(ra_.effectiveDate) == pd.Timestamp(rb_.effectiveDate)):
                keys.add(k)
        for old_id, new_id in keys:
            fd = obs.first_day(int(new_id))
            for tid in (int(old_id), int(new_id)):
                if fd is not None:
                    sc.add_window(tid, fd - LINEAGE_TOL_DAYS, fd + LINEAGE_TOL_DAYS, "lineage", f"pair {old_id}->{new_id}")
                row = b.loc[(old_id, new_id)] if (old_id, new_id) in b.index else a.loc[(old_id, new_id)]
                if pd.notna(row.effectiveDate):
                    ed = int(np.datetime64(pd.Timestamp(row.effectiveDate), "D").astype(np.int64))
                    sc.add_window(tid, ed - LINEAGE_TOL_DAYS, ed + LINEAGE_TOL_DAYS, "lineage", f"pair {old_id}->{new_id} effectiveDate")
                ld_ = obs.last_day(int(old_id))
                if ld_ is not None:
                    sc.add_window(tid, ld_ - LINEAGE_TOL_DAYS, ld_ + LINEAGE_TOL_DAYS, "lineage", f"pair {old_id}->{new_id} old last")
        res["lineage_pairs_changed"] = len(keys)
    # lifecycle revisions: tail after min(old, new) lastTradeDate; first rows for firstTradeDate changes
    if lifecycle_rev is not None and len(lifecycle_rev):
        for r in lifecycle_rev.itertuples(index=False):
            tid = int(r.tradingItemId)
            if r.field == "lastTradeDate":
                ds = [pd.to_datetime(x, errors="coerce") for x in (r.old, r.new)]
                dd = [int(np.datetime64(d, "D").astype(np.int64)) for d in ds if pd.notna(d)]
                if dd:
                    # bounded: only the rows between the old and the new lastTradeDate enter or leave the mask (the daily advance of a live
                    # name gives a window of a few days; its new-date cells are attributed by new_date anyway)
                    sc.add_window(tid, min(dd), max(dd) + 1, "lifecycle", "lastTradeDate moved (mask window)")
                    if pd.isna(ds[0]) or pd.isna(ds[1]):
                        sc.add_window(tid, min(dd) - 1, 1 << 40, "lifecycle", "lastTradeDate set/unset")
                else:
                    sc.add_window(tid, -(1 << 40), 1 << 40, "lifecycle", "lastTradeDate (no dates)")
                link_windows(tid, "lifecycle", "old lastTradeDate")
            elif r.field in ("status", "endReason"):
                continue                                   # no effect on returns_total (mask uses lastTradeDate)
            elif r.field == "firstTradeDate":
                ds = [pd.to_datetime(x, errors="coerce") for x in (r.old, r.new)]
                ds = [int(np.datetime64(d, "D").astype(np.int64)) for d in ds if pd.notna(d)]
                fd = obs.first_day(tid)
                pts = ds + ([fd] if fd is not None else [])
                if pts:
                    sc.add_window(tid, min(pts) - LINEAGE_TOL_DAYS, max(pts) + LINEAGE_TOL_DAYS, "lifecycle", "firstTradeDate (link row moved)")
                else:
                    sc.add_window(tid, -(1 << 40), 1 << 40, "lifecycle", "firstTradeDate (no dates)")
        # lifecycle ids removed / added
    if prev_lc is not None:
        gone = set(prev_lc.tradingItemId.astype(int)) - set(lc_new.tradingItemId.astype(int))
        for tid in gone:
            sc.add_window(tid, -(1 << 40), 1 << 40, "lifecycle", "id_removed")
    for tid in ctx.new_ids:
        sc.add_window(int(tid), -(1 << 40), 1 << 40, "new_id", "U1 new id")
    for tid in getattr(ctx, "aux_new_ids", []):
        sc.add_window(int(tid), -(1 << 40), 1 << 40, "aux_new", "aux addition")
    stale = getattr(ctx, "ledger_stale_after", None)
    sc.build()
    # attribute
    ids = diff.tradingItemId.to_numpy(np.int64)
    days = diff.day.to_numpy(np.int64)
    attr, how = [], []
    kinds = diff.kind.to_numpy()
    for tid, day, kind in zip(ids, days, kinds):
        hits = []
        if day > prev_asof_day and kind == "added":
            hits.append(("new_date", "row dated after the previous asOf"))
        if stale is not None and day > int(np.datetime64(pd.Timestamp(stale), "D").astype(np.int64)):
            hits.append(("stale_cut", "after ledger stale date"))
        hits += sc.lookup(int(tid), int(day))
        attr.append(";".join(dict.fromkeys(h[0] for h in hits)))
        how.append(hits[0][1] if hits else "")
    diff["attributedTo"] = attr
    diff["how"] = how
    diff["Top500"] = False
    un = diff[diff.attributedTo == ""]
    diff.to_parquet(out.t2_qa / "returns_attr_diff.parquet", index=False)
    if len(un):
        un.to_csv(out.t2_qa / "returns_attr_unattributed.csv", index=False)
    by_attr = diff.attributedTo.str.split(";").str[0].replace("", "UNATTRIBUTED").value_counts().to_dict()
    res.update({"pass": len(un) == 0, "unattributed": int(len(un)), "by_attribution": by_attr,
                "unattributed_examples": un.head(20)[["tradingItemId", "date", "retOld", "retNew", "flagOld", "flagNew", "kind"]].to_dict("records"),
                "ledger_rows_changed": n_ledger_rows, "seconds": round(time.time() - t0, 1)})
    return res


# ======================================================================================
# exposure summary (qa/exposure_daily.md)
# ======================================================================================
def load_book(pattern: str):
    fs = sorted(glob.glob(pattern))
    if not fs:
        return None, None
    pv = pd.read_parquet(fs[-1])
    pv.index = pd.to_datetime(pv.index)
    pv = pv.sort_index().fillna(0.0)
    pv.columns = pv.columns.astype(int)
    return fs[-1], pv


def exposure_daily(ctx, prev_events: pd.DataFrame | None, events: pd.DataFrame, prev_export: Path | None, export: Path | None, book_glob: str) -> dict:
    """qa/exposure_daily.md: (1) rows of the new asOf in returns_total by retFlag, list of flag 3/4/NaN rows; (2) live book (latest
    *_pos_values.parquet: the last book date's non-zero positions) x (a) asOf rows flagged 3/4/NaN, (b) settlements without terms that are
    new vs the previous build (events rows: settlementReturn NaN and no successor), (c) display rows (engine export markerReason) new vs
    the previous export, (d) held ids whose asOf row is missing; (3) totals. Blocking with --strict-exposure when a held id is hit."""
    out, prev = ctx.out, ctx.prev
    t = pq.read_table(out.returns, columns=["date", "tradingItemId", "ret", "retFlag"])
    d = t.column("date").to_numpy().astype("datetime64[ns]")
    asof = np.datetime64(ctx.asof, "ns")
    m = d == asof
    ids = t.column("tradingItemId").to_numpy()[m]
    ret = t.column("ret").to_numpy(zero_copy_only=False)[m]
    flag = t.column("retFlag").to_numpy()[m]
    cur = pd.read_parquet(out.master_file("current"), columns=["tradingItemId", "symbol"]).set_index("tradingItemId").symbol
    aux = pd.read_parquet(out.master_file("aux_instruments"), columns=["tradingItemId", "symbol"]).set_index("tradingItemId").symbol if out.master_file("aux_instruments").exists() else pd.Series(dtype=object)
    sym = pd.concat([cur, aux[~aux.index.isin(cur.index)]])
    lc = pd.read_parquet(out.master_file("lifecycle"), columns=["tradingItemId", "lastTradeDate", "status"]).set_index("tradingItemId")
    memb_p = out.universe
    top500 = set()
    if memb_p.exists():
        mb_ = pd.read_parquet(memb_p)
        mb_ = mb_[(mb_.universe == "Top500") & (mb_.fromDate <= ctx.asof) & (mb_.toDate >= ctx.asof)]
        top500 = set(int(i) for i in mb_.tradingItemId)
    asof_rows = pd.DataFrame({"tradingItemId": ids, "ret": ret, "retFlag": flag})
    asof_rows["symbol"] = asof_rows.tradingItemId.map(sym)
    asof_rows["Top500"] = asof_rows.tradingItemId.isin(list(top500))
    asof_rows["kind"] = np.where(np.isnan(asof_rows.ret), "nan", np.where(asof_rows.retFlag == 4, "flag4", np.where(asof_rows.retFlag == 3, "flag3", np.where(np.abs(asof_rows.ret) > 1.0, "big_move", "ok"))))
    flagged = asof_rows[asof_rows.kind != "ok"].sort_values(["kind", "tradingItemId"])
    flagged.to_csv(out.qa / "exposure_asof_flagged.csv", index=False)
    res = {"asOf": str(ctx.asof.date()), "asof_rows": int(len(asof_rows)), "asof_by_flag": asof_rows.retFlag.value_counts().sort_index().to_dict(),
           "asof_nan": int(np.isnan(ret).sum()), "asof_flag3": int((flag == 3).sum()), "asof_flag4": int((flag == 4).sum()),
           "asof_big_move_gt100pct": int((asof_rows.kind == "big_move").sum()), "asof_flagged_top500": int(flagged.Top500.sum())}
    # settlements without terms: new vs previous build
    ev = events.copy()
    ev["unconditioned"] = ev.settlementReturn.isna() & (ev.successorTradingItemId.isna() | (pd.to_numeric(ev.successorTradingItemId, errors="coerce").fillna(0) <= 0))
    unc = ev[ev.unconditioned]
    prev_unc_ids = set()
    if prev_events is not None and len(prev_events):
        pe = prev_events
        prev_unc_ids = set(int(i) for i in pe[pe.settlementReturn.isna() & (pe.successorTradingItemId.isna() | (pd.to_numeric(pe.successorTradingItemId, errors="coerce").fillna(0) <= 0))].tradingItemId)
    new_unc = unc[~unc.tradingItemId.isin(list(prev_unc_ids))].copy()
    new_unc["symbol"] = new_unc.tradingItemId.map(sym)
    ln_succ = pd.read_parquet(out.master_file("lineage"), columns=["oldId", "newId", "relation"]) if out.master_file("lineage").exists() else pd.DataFrame(columns=["oldId", "newId", "relation"])
    succ_of = {int(a): f"{b}({r})" for a, b, r in zip(ln_succ.oldId, ln_succ.newId, ln_succ.relation)}
    new_unc["lineageSuccessor"] = new_unc.tradingItemId.map(succ_of).fillna("")          # a relist/rename with a lineage successor is not a settlement without terms for the returns
    new_unc[["tradingItemId", "symbol", "endDate", "settlementDate", "actionCode", "cacheUnitStatus", "endDateSource", "lineageSuccessor"]].to_csv(out.qa / "exposure_new_unconditioned_settlements.csv", index=False)
    gone_unc = sorted(int(i) for i in (prev_unc_ids - set(int(i) for i in unc.tradingItemId)))
    res["unconditioned_settlements"] = {"total": int(len(unc)), "prev_total": len(prev_unc_ids), "new_vs_prev": int(len(new_unc)), "gone_vs_prev": len(gone_unc),
                                        "new_ids": sorted(int(i) for i in new_unc.tradingItemId)[:50], "gone_ids": gone_unc[:50]}
    # display rows (export markerReason): new vs previous export
    marker_new = pd.DataFrame()
    if export is not None and Path(export).exists():
        ex = pd.read_parquet(export)
        if "markerReason" in ex.columns:
            marker_new = ex[ex.markerReason.fillna("").astype(str) != ""][["tradingItemId", "exDate", "eventType", "legType", "markerReason"]].copy()
            if prev_export is not None and Path(prev_export).exists():
                pex = pd.read_parquet(prev_export)
                if "markerReason" in pex.columns:
                    pk = set(zip(pex.tradingItemId.astype(int), pd.to_datetime(pex.exDate).dt.strftime("%Y-%m-%d"), pex.legType.astype(str)))
                    marker_new["isNew"] = [(int(a), pd.Timestamp(b).strftime("%Y-%m-%d"), str(c)) not in pk for a, b, c in zip(marker_new.tradingItemId, marker_new.exDate, marker_new.legType)]
                else:
                    marker_new["isNew"] = True
            else:
                marker_new["isNew"] = True
            marker_new["tradingItemId"] = pd.to_numeric(marker_new.tradingItemId).astype("int64")          # export ids are strings (P2-4)
            marker_new["symbol"] = marker_new.tradingItemId.map(sym)
            marker_new.to_csv(out.qa / "exposure_display_rows.csv", index=False)
    res["display_rows"] = {"total": int(len(marker_new)), "new_vs_prev": int(marker_new.isNew.sum()) if len(marker_new) else 0}
    # live book
    book_file, pv = load_book(book_glob)
    held = pd.DataFrame(columns=["tradingItemId", "pos"])
    book_date = None
    if pv is not None and len(pv):
        upto = pv.index[pv.index <= pd.Timestamp(ctx.asof) + pd.Timedelta(days=7)]
        book_date = (upto[-1] if len(upto) else pv.index[-1])
        row = pv.loc[book_date]
        held = row[row != 0].rename("pos").reset_index().rename(columns={"index": "tradingItemId"})
        held["tradingItemId"] = held.tradingItemId.astype(int)
    held_ids = set(int(i) for i in held.tradingItemId)
    hits = []
    for r in flagged.itertuples(index=False):
        if int(r.tradingItemId) in held_ids:
            hits.append({"tradingItemId": int(r.tradingItemId), "symbol": r.symbol, "kind": f"asof_{r.kind}", "retFlag": int(r.retFlag), "ret": float(r.ret) if pd.notna(r.ret) else None,
                         "pos": float(held.set_index("tradingItemId").pos.get(int(r.tradingItemId), 0.0))})
    for r in new_unc.itertuples(index=False):
        if int(r.tradingItemId) in held_ids:
            hits.append({"tradingItemId": int(r.tradingItemId), "symbol": r.symbol, "kind": "new_unconditioned_settlement", "retFlag": None, "ret": None,
                         "pos": float(held.set_index("tradingItemId").pos.get(int(r.tradingItemId), 0.0))})
    if len(marker_new):
        for r in marker_new[marker_new.isNew].itertuples(index=False):
            if int(r.tradingItemId) in held_ids:
                hits.append({"tradingItemId": int(r.tradingItemId), "symbol": r.symbol, "kind": f"new_display_row:{r.markerReason}", "retFlag": None, "ret": None,
                             "pos": float(held.set_index("tradingItemId").pos.get(int(r.tradingItemId), 0.0))})
    missing_row = sorted(held_ids - set(int(i) for i in asof_rows.tradingItemId))
    ended = [i for i in missing_row if i in lc.index and pd.notna(lc.at[i, "lastTradeDate"]) and pd.Timestamp(lc.at[i, "lastTradeDate"]) < pd.Timestamp(ctx.asof)]
    for i in ended:
        hits.append({"tradingItemId": int(i), "symbol": sym.get(i), "kind": "held_after_lastTradeDate", "retFlag": None, "ret": None, "pos": float(held.set_index("tradingItemId").pos.get(int(i), 0.0))})
    hits_df = pd.DataFrame(hits, columns=["tradingItemId", "symbol", "kind", "retFlag", "ret", "pos"])
    hits_df.to_csv(out.qa / "exposure_book_hits.csv", index=False)
    gross = float(held.pos.abs().sum()) if len(held) else 0.0
    res["book"] = {"file": book_file, "book_date": str(pd.Timestamp(book_date).date()) if book_date is not None else None, "held_ids": len(held_ids), "gross_abs": gross,
                   "hits": int(len(hits_df)), "hits_abs_pos": float(hits_df.pos.abs().sum()) if len(hits_df) else 0.0, "held_ids_without_asof_row": len(missing_row),
                   "held_after_lastTradeDate": len(ended), "hit_kinds": hits_df.kind.value_counts().to_dict() if len(hits_df) else {}}
    strict = bool(getattr(ctx.args, "strict_exposure", False))
    res["pass"] = (len(hits_df) == 0) if strict else True
    res["warning"] = len(hits_df) > 0
    res["blocking"] = strict
    # markdown
    L = [f"# exposure_daily - asOf {ctx.asof.date()} (build {ctx.build_id}, observed {ctx.observed.date()})", "",
         f"- asOf rows: {len(asof_rows):,} | retFlag {res['asof_by_flag']} | NaN {res['asof_nan']} | flag3 {res['asof_flag3']} | flag4 {res['asof_flag4']} | |ret|>100% {res['asof_big_move_gt100pct']} (Top500 flagged {res['asof_flagged_top500']})",
         f"- settlements without terms: {len(unc):,} total (previous build {len(prev_unc_ids):,}), {len(new_unc)} new / {len(gone_unc)} gone vs previous build (exposure_new_unconditioned_settlements.csv; lineageSuccessor = relist/rename with a successor)",
         f"- engine display rows (markerReason): {res['display_rows']['total']} total, {res['display_rows']['new_vs_prev']} new vs previous export (exposure_display_rows.csv)",
         f"- live book: {book_file} @ {res['book']['book_date']} held {len(held_ids)} ids gross {gross:,.0f} -> hits {len(hits_df)} (abs pos {res['book']['hits_abs_pos']:,.0f}); "
         f"held ids without asOf row {len(missing_row)} (after lastTradeDate {len(ended)})",
         f"- gate G-EXPO: {'PASS' if res['pass'] else 'FAIL'} ({'blocking (--strict-exposure)' if strict else 'warning only'})", ""]
    if len(flagged):
        L += ["## asOf rows flagged 3/4/NaN/|ret|>100% (first 60)", "", md(flagged.head(60)[["tradingItemId", "symbol", "kind", "retFlag", "ret", "Top500"]]), ""]
    if len(hits_df):
        L += ["## live book hits", "", md(hits_df), ""]
    if len(new_unc):
        L += ["## new settlements without terms", "", md(new_unc.head(40)[["tradingItemId", "symbol", "endDate", "settlementDate", "actionCode", "cacheUnitStatus", "lineageSuccessor"]]), ""]
    if len(marker_new) and marker_new.isNew.any():
        L += ["## new display rows", "", md(marker_new[marker_new.isNew].head(40)), ""]
    (out.qa / "exposure_daily.md").write_text("\n".join(L))
    return res


def md(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    L = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in df.itertuples(index=False):
        L.append("| " + " | ".join("" if (isinstance(v, float) and np.isnan(v)) or v is None else (f"{v:.6g}" if isinstance(v, float) else str(v)) for v in r) + " |")
    return "\n".join(L)


# ======================================================================================
# publish view: a stage dir publish.py reads (--stage) + an rc2 stage (--rc2-stage) built from the daily build
# ======================================================================================
def _link(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink() or dst.exists():
        if dst.is_dir() and not dst.is_symlink():
            shutil.rmtree(dst)
        else:
            dst.unlink()
    os.symlink(Path(src).resolve(), dst)


def build_stage_view(ctx) -> Path:
    """Published-layout view of the new build (symlinks): base_data/factors/derived at the root, master/, events/, universe/,
    calendar.parquet. Used by returns_v2_qa (--build), the ledger builder (--prev of the next day) and the exposure step."""
    out = ctx.out
    v = out.root / "view"
    v.mkdir(exist_ok=True)
    _link(out.base_data, v / "base_data.parquet")
    _link(out.t2 / "factors", v / "factors")
    _link(out.t2 / "derived", v / "derived")
    _link(out.t1 / "master", v / "master")
    if (out.t6 / "events").exists():
        _link(out.t6 / "events", v / "events")
    if (out.t5 / "universe").exists():
        _link(out.t5 / "universe", v / "universe")
    if out.calendar.exists():
        _link(out.calendar, v / "calendar.parquet")
    for name in ("dividends", "distributions"):
        src = ctx.prev.root / name
        if src.exists():
            _link(src, v / name)
    if out.build_json.exists():
        _link(out.build_json, v / "BUILD.json")
    return v


def prev_published_view(ctx) -> Path:
    """The previous build as a published layout (its own root when published; a symlink view for a stage build)."""
    prev = ctx.prev
    if prev.layout == "published":
        return prev.root
    v = ctx.out.rc2f / "prev_view"
    v.mkdir(parents=True, exist_ok=True)
    _link(prev.base_data, v / "base_data.parquet")
    _link(prev.t2 / "factors", v / "factors")
    _link(prev.t2 / "derived", v / "derived")
    _link(prev.master, v / "master")
    if (prev.t6 / "events").exists():
        _link(prev.t6 / "events", v / "events")
    if (prev.t5 / "universe").exists():
        _link(prev.t5 / "universe", v / "universe")
    if prev.calendar.exists():
        _link(prev.calendar, v / "calendar.parquet")
    for name in ("dividends", "distributions"):
        for src in (prev.root / name, prev.root / "view" / name):
            if src.exists():
                _link(src, v / name)
                break
    if prev.build_json.exists():
        _link(prev.build_json, v / "BUILD.json")
    return v


def build_publish_view(ctx, alog_path: Path, info_path: Path) -> tuple[Path, Path]:
    """(stage view, rc2 stage) for `publish.py --stage <view> --t2 <T2> --rc2-stage <rc2>`:
    view/T1/master = the daily master with lifecycle = instruments only and tradable_exclusions without aux rows (publish.integrate_rc2
    appends R3/master/lifecycle_aux and the aux exclusions itself); view/T2_work, T5, T6, qa dirs = symlinks.
    rc2/R1/events = the ledger files, rc2/R3/master = lifecycle (instruments) / lifecycle_aux / lifecycle_dates / lineage / aux_instruments,
    rc2/R3/events/corporate_actions_reclass.parquet = the daily events, rc2/{rc2f_apply_log.csv, rc2f_apply_info.json, overrides/, recount/}."""
    out = ctx.out
    view = out.root / "publish_view"
    rc2 = out.root / "rc2_stage"
    for d in (view, rc2):
        if d.exists():
            shutil.rmtree(d)
    (view / "T1" / "master").mkdir(parents=True)
    for p in sorted(out.master.glob("*.parquet")):
        if p.name in ("lifecycle.parquet", "tradable_exclusions.parquet", "lifecycle_aux.parquet"):
            continue
        _link(p, view / "T1" / "master" / p.name)
    lc_instr = out.t1_work / "lifecycle_instruments.parquet"
    ex_instr = out.t1_work / "tradable_exclusions_instruments.parquet"
    if not lc_instr.exists():
        lc = pd.read_parquet(out.master_file("lifecycle"))
        lc[lc.status != "aux"].reset_index(drop=True).to_parquet(lc_instr, index=False)
    if not ex_instr.exists():
        ex = pd.read_parquet(out.master_file("tradable_exclusions"))
        ex[ex.reason != "aux_instrument"].reset_index(drop=True).to_parquet(ex_instr, index=False)
    _link(lc_instr, view / "T1" / "master" / "lifecycle.parquet")
    _link(ex_instr, view / "T1" / "master" / "tradable_exclusions.parquet")
    for name in ("_work", "qa"):
        if (out.t1 / name).exists():
            _link(out.t1 / name, view / "T1" / name)
    _link(out.t2_work, view / "T2_work")
    _link(out.t2, view / "T2")
    for t in ("T5", "T6"):
        if (out.root / t).exists():
            _link(out.root / t, view / t)
    if out.calendar.exists():
        _link(out.calendar, view / "calendar.parquet")
    (rc2 / "R1" / "events").mkdir(parents=True)
    (rc2 / "R3" / "master").mkdir(parents=True)
    (rc2 / "R3" / "events").mkdir(parents=True)
    for p in sorted((out.t6 / "events").glob("ca_ledger*")):
        _link(p, rc2 / "R1" / "events" / p.name)
    _link(lc_instr, rc2 / "R3" / "master" / "lifecycle.parquet")
    for name in ("lifecycle_aux", "lifecycle_dates", "lineage", "aux_instruments"):
        if out.master_file(name).exists():
            _link(out.master_file(name), rc2 / "R3" / "master" / f"{name}.parquet")
    _link(out.events, rc2 / "R3" / "events" / "corporate_actions_reclass.parquet")
    _link(alog_path, rc2 / "rc2f_apply_log.csv")
    _link(info_path, rc2 / "rc2f_apply_info.json")
    if (out.rc2f / "overrides").exists():
        _link(out.rc2f / "overrides", rc2 / "overrides")
    rec = rc2 / "recount"
    rec.mkdir()
    for p in (out.t2_qa / "returns_attr_diff.parquet", out.t2_qa / "returns_attr_unattributed.csv", out.t2_qa / "returns_attr_ledger_diff.csv", out.qa / "exposure_daily.md"):
        if p.exists():
            _link(p, rec / p.name)
    return view, rc2


def anchor_check(pub_root: Path, read_rows) -> dict:
    """Engine export anchors vs the published panel (the C++ loader refuses a stale file): events_cache successorCacheClose at
    successorCacheCloseDate and targetCacheMark at targetCacheMarkDate must equal the split_adj close of the panel; ca_ledger_cache
    childCloseAtExCache (child in panel, status ok, not a marker) at exDate must equal the child's split_adj close."""
    from prices_factors import FactorLookup
    pub_root = Path(pub_root)
    res = {}
    sp = FactorLookup(pd.read_parquet(pub_root / "factors" / "split.parquet"))
    ev_p = pub_root / "engine" / "events_cache.parquet"
    if ev_p.exists():
        ev = pd.read_parquet(ev_p)
        chk = []
        for id_col, close_col, date_col in (("successorTradingItemId", "successorCacheClose", "successorCacheCloseDate"), ("tradingItemId", "targetCacheMark", "targetCacheMarkDate")):
            sub = ev[ev[close_col].notna() & ev[date_col].notna() & pd.to_numeric(ev[id_col], errors="coerce").notna()]
            if not len(sub):
                continue
            ids = pd.to_numeric(sub[id_col]).astype(np.int64).to_numpy()
            rows = read_rows(pub_root / "base_data.parquet", ["tradingItemId", "date", "close"], ids=np.unique(ids))
            rows = rows.set_index(["tradingItemId", "date"]).close
            dates = pd.to_datetime(sub[date_col]).to_numpy().astype("datetime64[ns]")
            raw = rows.reindex(list(zip(ids, dates))).to_numpy(dtype=np.float64)
            adj = raw / sp(ids, dates, strict=False)
            exp = sub[close_col].to_numpy(dtype=np.float64)
            with np.errstate(invalid="ignore", divide="ignore"):
                bad = ~(np.abs(adj / exp - 1.0) <= 1e-6)
            chk.append({"column": close_col, "rows": int(len(sub)), "mismatch": int(np.nansum(bad)), "missing_in_panel": int(np.isnan(raw).sum())})
        res["events_cache"] = chk
    led_p = pub_root / "engine" / "ca_ledger_cache.parquet"
    if led_p.exists():
        L = pd.read_parquet(led_p)
        sub = L[L.childCloseAtExCache.notna() & pd.to_numeric(L.childTradingItemId, errors="coerce").notna() & (L.status == "ok")]
        if "markerReason" in L.columns:
            sub = sub[sub.markerReason.fillna("").astype(str) == ""]
        if "childValueSource" in L.columns:
            sub = sub[sub.childValueSource.astype(str) == "child_close_ex"]
        if len(sub):
            ids = pd.to_numeric(sub.childTradingItemId).astype(np.int64).to_numpy()
            dates = pd.to_datetime(sub.exDate).to_numpy().astype("datetime64[ns]")
            rows = read_rows(pub_root / "base_data.parquet", ["tradingItemId", "date", "close"], ids=np.unique(ids)).set_index(["tradingItemId", "date"]).close
            raw = rows.reindex(list(zip(ids, dates))).to_numpy(dtype=np.float64)
            adj = raw / sp(ids, dates, strict=False)
            exp = sub.childCloseAtExCache.to_numpy(dtype=np.float64)
            with np.errstate(invalid="ignore", divide="ignore"):
                bad = ~(np.abs(adj / exp - 1.0) <= 1e-6) & np.isfinite(raw)
            res["ca_ledger_cache"] = {"rows": int(len(sub)), "mismatch": int(bad.sum()), "missing_in_panel": int(np.isnan(raw).sum())}
    res["pass"] = all(c["mismatch"] == 0 for c in res.get("events_cache", [])) and res.get("ca_ledger_cache", {}).get("mismatch", 0) == 0
    return res


# ======================================================================================
# R-D2 rework (2026-09-21): P0-1 factors of kept restatements, P0-2 events delta, P1-1 first dates, P1-2 idempotent text,
# P1-5 narrow attribution + value-sanity gates, P2-7 same-day event marker / mi lag
# ======================================================================================
LOG4 = float(np.log(4.0))


def carry_factors_for_kept(prev_split: pd.DataFrame, vendor_split: pd.DataFrame, kept_ids, prev_asof: pd.Timestamp, cal, observed: pd.Timestamp) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """P0-1: a restated id whose stored raw is kept must keep its factor intervals too (raw and factors are one basis). The previous
    build's intervals are carried; vendor boundaries dated after the previous asOf (a new split) are appended with rc2f_apply._split_one
    'add' semantics (earlier intervals rescaled, source tag |daily_kept_add), so a genuine new split is not lost while the restated history
    is not adopted. Returns (carried table for the kept ids, vendor intervals of those ids (for the revisions log), info)."""
    kept = sorted(int(i) for i in kept_ids)
    if not kept:
        return prev_split.iloc[0:0].copy(), vendor_split.iloc[0:0].copy(), {"ids": 0}
    prev_k = prev_split[prev_split.tradingItemId.isin(kept)].copy()
    vend_k = vendor_split[vendor_split.tradingItemId.isin(kept)].copy()
    out, added, missing = [], 0, []
    for tid in kept:
        g = prev_k[prev_k.tradingItemId == tid].sort_values("validFrom").reset_index(drop=True)
        if not len(g):
            missing.append(tid)
            continue
        v = vend_k[vend_k.tradingItemId == tid].sort_values("validFrom").reset_index(drop=True)
        fac = v.factor.to_numpy(dtype=float)
        for j in range(1, len(v)):
            vf = pd.Timestamp(v.validFrom.iloc[j])
            if vf <= prev_asof or fac[j] == fac[j - 1]:
                continue
            pf = float(fac[j - 1] / fac[j])                          # parentFactor of the new vendor boundary
            g2, n, reason = ra._split_one(g, {"exDate": vf, "action": "add", "newFactor": pf, "oid": "daily_kept_add", "newExDate": None, "estimate": False}, cal)
            if n:
                g = g2.sort_values("validFrom").reset_index(drop=True)
                added += 1
        g["snapshotDate"] = pd.Timestamp(observed)
        out.append(g)
    carried = pd.concat(out, ignore_index=True) if out else prev_k.iloc[0:0].copy()
    for c in ("validFrom", "validTo", "snapshotDate"):
        carried[c] = pd.to_datetime(carried[c]).astype("datetime64[ns]")
    carried["factor"] = carried.factor.astype("float64")
    carried["tradingItemId"] = carried.tradingItemId.astype("int64")
    return carried, vend_k, {"ids": len(kept), "carried_ids": int(carried.tradingItemId.nunique()), "new_boundaries_added": added, "ids_without_prev_intervals": missing}


def side_from_intervals(ids, dates, split_table: pd.DataFrame) -> np.ndarray:
    from prices_factors import FactorLookup
    return FactorLookup(split_table)(np.asarray(ids, dtype=np.int64), np.asarray(dates).astype("datetime64[ns]"), strict=False)


def factor_boundary_jumps(split_new: pd.DataFrame, factor_rev: pd.DataFrame | None, base_path: Path, cal, asof: pd.Timestamp, max_log: float = LOG4) -> dict:
    """G-FACTOR-JUMP: at every split boundary added/changed today (validFrom <= asOf) the raw close jump x parentFactor must be ~1
    (|log| <= log 4); a violation means raw and factors are on different bases (P0-1 class). Blocking."""
    import ledger_build as lb
    res = {"boundaries": 0, "checked": 0, "violations": 0, "uncheckable": 0, "examples": []}
    if factor_rev is None or not len(factor_rev):
        return {**res, "pass": True}
    fr = factor_rev[factor_rev.table.astype(str).str.startswith("split") & factor_rev.kind.isin(["added", "changed"])]
    fr = fr[pd.to_datetime(fr.validFrom) <= pd.Timestamp(asof)]
    fr = fr[pd.to_datetime(fr.validFrom) > pd.Timestamp("1900-01-02")]
    res["boundaries"] = int(len(fr))
    if not len(fr):
        return {**res, "pass": True}
    ids = set(int(i) for i in fr.tradingItemId)
    closes = lb.RawCloses([base_path], ids)
    ex = []
    for r in fr.itertuples(index=False):
        tid = int(r.tradingItemId)
        g = split_new[split_new.tradingItemId == tid].sort_values("validFrom").reset_index(drop=True)
        vf = pd.to_datetime(g.validFrom)
        j = int(np.flatnonzero((vf == pd.Timestamp(r.validFrom)).to_numpy())[0]) if (vf == pd.Timestamp(r.validFrom)).any() else -1
        if j <= 0:
            continue
        pf = float(g.factor.iloc[j - 1]) / float(g.factor.iloc[j])
        jump, why = ra._raw_jump(closes, tid, pd.Timestamp(r.validFrom), cal)
        if not np.isfinite(jump) or jump <= 0:
            res["uncheckable"] += 1
            continue
        res["checked"] += 1
        v = abs(float(np.log(jump * pf)))
        if v > max_log:
            res["violations"] += 1
            if len(ex) < 20:
                ex.append({"tradingItemId": tid, "validFrom": str(pd.Timestamp(r.validFrom).date()), "parentFactor": pf, "rawJump": jump, "log_jump_x_pf": v, "kind": r.kind})
    res["examples"] = ex
    res["pass"] = res["violations"] == 0
    return res


def events_delta_ids(lc_prev: pd.DataFrame, lc_fresh: pd.DataFrame, prev_events: pd.DataFrame | None, fresh_events: pd.DataFrame | None,
                     prev_asof: pd.Timestamp, asof: pd.Timestamp, override_ids: set) -> dict:
    """P0-2: which ids' events rows may be taken from today's T6 output: newly ended ids (in fresh, not in the previous events), ids whose
    lifecycle end changed today (lastTradeDate / status; the daily advance prev asOf -> asOf of an active name is not a change), and ids that
    came back to life (in the previous events but active now: row dropped). Ids with an rc2f lifecycle override are never replaced."""
    a = lc_prev.set_index("tradingItemId")
    b = lc_fresh.set_index("tradingItemId")
    common = a.index.intersection(b.index)
    la, lb_ = a.loc[common, "lastTradeDate"], b.loc[common, "lastTradeDate"]
    sa, sb = a.loc[common, "status"].astype(str), b.loc[common, "status"].astype(str)
    trivial = (sa == "active") & (sb == "active") & (pd.to_datetime(la) <= pd.Timestamp(prev_asof)) & (pd.to_datetime(lb_) <= pd.Timestamp(asof)) & (pd.to_datetime(lb_) >= pd.to_datetime(la))
    changed = ~((la.isna() & lb_.isna()) | (la == lb_)) & ~trivial
    changed |= (sa != sb) & ~((sa == "active") & (sb == "active"))
    changed_ids = set(int(i) for i in common[changed.to_numpy()])
    prev_ids = set(int(i) for i in prev_events.tradingItemId) if prev_events is not None and len(prev_events) else set()
    fresh_ids = set(int(i) for i in fresh_events.tradingItemId) if fresh_events is not None and len(fresh_events) else set()
    new_ended = fresh_ids - prev_ids
    revived = set(int(i) for i in common[(sb == "active").to_numpy()]) & prev_ids
    delta = (changed_ids | new_ended | revived) - set(int(i) for i in override_ids)
    return {"delta": delta, "changed_lifecycle": changed_ids, "new_ended": new_ended, "revived": revived, "excluded_override": (changed_ids | new_ended | revived) & set(int(i) for i in override_ids)}


EV_CMP = ["endDate", "settlementDate", "actionCode", "settlementReturn", "cashPerShare", "successorTradingItemId", "successorRatio", "hasCashLeg", "hasStockLeg",
          "cacheUnitStatus", "endDateSource", "source", "overrideSource", "settlementReturnScope"]


def events_delta_merge(fresh: pd.DataFrame | None, prev: pd.DataFrame | None, delta: set, out_csv: Path | None = None) -> tuple[pd.DataFrame, dict]:
    """P0-2: events of the day = the previous build's events (every row carried, rc2f lifecycle overrides / R3 reclass / labels included)
    with only the delta ids replaced by today's T6 rows (or dropped when T6 has none). Every fresh row that differs from the carried one
    but is NOT adopted is listed (transitions csv, kind not_adopted) - the fixed snapshot is not re-litigated by the daily."""
    if prev is None or not len(prev):
        ev = fresh.copy() if fresh is not None else pd.DataFrame()
        if "settlementReturnScope" not in ev.columns:
            ev["settlementReturnScope"] = "full"
        return ra._events_types(ev), {"mode": "fresh(no previous events)", "rows": int(len(ev))}
    prev = prev.copy()
    if "settlementReturnScope" not in prev.columns:
        prev["settlementReturnScope"] = "full"
    fresh = fresh.copy() if fresh is not None else prev.iloc[0:0].copy()
    if "settlementReturnScope" not in fresh.columns:
        fresh["settlementReturnScope"] = "full"
    pk = prev.set_index("tradingItemId")
    fk = fresh.set_index("tradingItemId")
    rows, adopted, dropped, not_adopted = [], [], [], []

    def diff_fields(a, b):
        out = []
        for c in EV_CMP:
            if c not in a.index or c not in b.index:
                continue
            x, y = a[c], b[c]
            same = (pd.isna(x) and pd.isna(y)) or (str(x) == str(y)) or (isinstance(x, float) and isinstance(y, float) and np.isfinite(x) and np.isfinite(y) and abs(x - y) <= 1e-9)
            if not same:
                out.append(f"{c}:{x}->{y}")
        return out
    for tid in pk.index:
        t = int(tid)
        if t in delta:
            if t in fk.index:
                row = fk.loc[t]
                d = diff_fields(pk.loc[t], row)
                adopted.append({"tradingItemId": t, "kind": "adopted_changed" if d else "adopted_same", "fields": ";".join(d)[:400]})
                rows.append(row)
            else:
                dropped.append({"tradingItemId": t, "kind": "dropped(no T6 row today)", "fields": f"endDate:{pk.loc[t].endDate}->"})
        else:
            rows.append(pk.loc[t])
            if t in fk.index:
                d = diff_fields(pk.loc[t], fk.loc[t])
                if d:
                    not_adopted.append({"tradingItemId": t, "kind": "not_adopted", "fields": ";".join(d)[:400]})
    for tid in fk.index:
        t = int(tid)
        if t not in pk.index:
            if t in delta:
                rows.append(fk.loc[t])
                adopted.append({"tradingItemId": t, "kind": "adopted_new", "fields": f"endDate:->{fk.loc[t].endDate}"})
            else:
                not_adopted.append({"tradingItemId": t, "kind": "not_adopted_new(no lifecycle change)", "fields": f"endDate:->{fk.loc[t].endDate}"})
    ev = pd.DataFrame(rows).reset_index().rename(columns={"index": "tradingItemId"}) if rows else prev.iloc[0:0].copy()
    ev = ev[prev.columns] if len(ev) else ev
    trans = pd.DataFrame(adopted + dropped + not_adopted, columns=["tradingItemId", "kind", "fields"])
    if out_csv is not None:
        trans.to_csv(out_csv, index=False)
    def unc(df):
        if df is None or not len(df):
            return 0
        s = pd.to_numeric(df.successorTradingItemId, errors="coerce")
        return int((df.settlementReturn.isna() & (s.isna() | (s.fillna(0) <= 0))).sum())
    info = {"mode": "delta", "prev_rows": int(len(prev)), "fresh_rows": int(len(fresh)), "rows": int(len(ev)), "delta_ids": len(delta),
            "adopted_changed": sum(1 for r in adopted if r["kind"] == "adopted_changed"), "adopted_same": sum(1 for r in adopted if r["kind"] == "adopted_same"),
            "adopted_new": sum(1 for r in adopted if r["kind"] == "adopted_new"), "dropped": len(dropped), "not_adopted": len(not_adopted),
            "unconditioned_prev": unc(prev), "unconditioned_new": unc(ev), "unconditioned_fresh_t6": unc(fresh)}
    return ra._events_types(ev), info


LN_VALUE_COLS = ["relation", "effectiveDate", "ratio", "confidence", "ratioSource", "linkClass", "chainGapTradingDays", "chainable", "linkVerified"]


def restore_unchanged_lineage_text(prev_ln: pd.DataFrame | None, ln: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """P1-2: rows whose value columns equal the carried previous row keep the previous text columns (evidence / version / sources /
    reviewFlag) so re-applied overrides never accumulate ' | rc2f:<id>: ...' fragments day after day."""
    if prev_ln is None or not len(prev_ln) or not len(ln):
        return ln, 0
    ln = ln.copy()
    a = prev_ln.set_index(["oldId", "newId"])
    b = ln.set_index(["oldId", "newId"])
    common = a.index.intersection(b.index)
    n = 0
    for k in common:
        ra_, rb_ = a.loc[k], b.loc[k]
        same = True
        for c in LN_VALUE_COLS:
            if c not in a.columns or c not in b.columns:
                continue
            x, y = ra_[c], rb_[c]
            if isinstance(x, float) or isinstance(y, float):
                if not ((pd.isna(x) and pd.isna(y)) or (pd.notna(x) and pd.notna(y) and abs(float(x) - float(y)) <= 1e-12)):
                    same = False
                    break
            elif not ((pd.isna(x) and pd.isna(y)) or str(x) == str(y)):
                same = False
                break
        if same:
            for c in ("evidence", "version", "sources", "reviewFlag"):
                if c in a.columns and c in b.columns and str(ra_[c]) != str(rb_[c]):
                    b.at[k, c] = ra_[c]
                    n += 1
    return b.reset_index()[ln.columns], n


def reclassify_delta(lc_fresh: pd.DataFrame, frame_full: pd.DataFrame | None, ev_final: pd.DataFrame, delta_ids: set, cal: np.ndarray, asof: pd.Timestamp) -> tuple[pd.DataFrame, int]:
    """P2-7: the U4 lifecycle used the previous day's events as marker; after the day's events delta the delta ids are classified again with
    their final events row (same-day marker)."""
    import master_lifecycle as ml
    if frame_full is None or not delta_ids:
        return lc_fresh, 0
    f = frame_full.set_index("tradingItemId")
    ids = [i for i in sorted(delta_ids) if i in f.index]
    if not ids:
        return lc_fresh, 0
    sub = f.loc[ids].reset_index()
    e = ev_final.set_index("tradingItemId")
    sub["eventEndDate"] = pd.to_datetime(sub.tradingItemId.map(e.endDate)).astype("datetime64[ns]")
    sub["eventActionCode"] = sub.tradingItemId.map(e.actionCode)
    sub["eventHasSuccessor"] = sub.tradingItemId.map(e.successorTradingItemId).notna()
    r = ml.classify(sub, cal, asof).set_index("tradingItemId")
    k = lc_fresh.set_index("tradingItemId")
    n = 0
    for tid in r.index:
        if tid not in k.index:
            continue
        for c in ("lastTradeDate", "lastTradeSource", "status", "endReason", "evidence", "eventEndDate"):
            if c in k.columns:
                k.at[tid, c] = r.at[tid, c]
        n += 1
    out = k.reset_index()
    for c in ("lastTradeDate", "eventEndDate"):
        out[c] = pd.to_datetime(out[c]).astype("datetime64[ns]")
    return out, n


def mi_lag_rule(lc: pd.DataFrame, cal: np.ndarray, asof: pd.Timestamp, max_tdays: int) -> tuple[pd.DataFrame, int]:
    """P2-7 (daily rule): an ACTIVE id whose lastTradeDate came from a lagging miprice (mi last close < ciq last close, within max_tdays
    trading days, ciq still printing) keeps its ciq last close as lastTradeDate (lastTradeSource 'ciq(mi-lag)'); otherwise a live quote of a
    held name would be masked for a day and come back the next (removed/added churn). Off with max_tdays = 0."""
    import master_lifecycle as ml
    if max_tdays <= 0 or not len(lc):
        return lc, 0
    lc = lc.copy()
    m = (lc.status.astype(str) == "active") & (lc.lastTradeSource.astype(str) == "mi") & lc.ciqLastCloseDate.notna() & lc.miLastCloseDate.notna() \
        & (pd.to_datetime(lc.ciqLastCloseDate) > pd.to_datetime(lc.miLastCloseDate))
    if not m.any():
        return lc, 0
    gap = ml.tdays_between(cal, pd.to_datetime(lc.loc[m, "miLastCloseDate"]).to_numpy().astype("datetime64[ns]"), pd.to_datetime(lc.loc[m, "ciqLastCloseDate"]).to_numpy().astype("datetime64[ns]"))
    idx = lc.index[m][np.asarray(gap) <= max_tdays]
    lc.loc[idx, "lastTradeDate"] = pd.to_datetime(lc.loc[idx, "ciqLastCloseDate"]).to_numpy().astype("datetime64[ns]")
    lc.loc[idx, "lastTradeSource"] = "ciq(mi-lag)"
    lc.loc[idx, "evidence"] = lc.loc[idx, "evidence"].astype(str) + " | daily: mi lags ciq by <= %d tdays -> ciq last close kept" % max_tdays
    lc["lastTradeDate"] = pd.to_datetime(lc.lastTradeDate).astype("datetime64[ns]")
    return lc, int(len(idx))


def dedupe_tags(series: pd.Series, sep: str = ";") -> pd.Series:
    """D3 (1): text tag columns are sets - a re-applied rule must not append the same token twice (order kept)."""
    def one(v):
        if not isinstance(v, str) or sep not in v:
            return v
        seen, out = set(), []
        for tok in v.split(sep):
            if tok not in seen:
                seen.add(tok)
                out.append(tok)
        return sep.join(out)
    return series.map(one)


def carry_last_dates_rowless(lc_new: pd.DataFrame, lc_prev: pd.DataFrame | None) -> tuple[pd.DataFrame, int]:
    """D3 (4): an id whose rows the vendor removed entirely has no close left -> lastTradeDate NaT breaks the universe builder; the previous
    build's last dates are carried (status provisional / data_gap, evidence notes it) until a marker or new rows arrive."""
    if lc_prev is None or not len(lc_new):
        return lc_new, 0
    k = lc_new.set_index("tradingItemId")
    p = lc_prev.set_index("tradingItemId")
    m = k.lastTradeDate.isna() & k.index.isin(p.index)
    n = 0
    for tid in k.index[m]:
        if pd.isna(p.at[tid, "lastTradeDate"]):
            continue
        for c in ("lastTradeDate", "ciqLastCloseDate", "miLastCloseDate", "firstTradeDate", "firstPriceDate"):
            if pd.isna(k.at[tid, c]) and pd.notna(p.at[tid, c]):
                k.at[tid, c] = p.at[tid, c]
        k.at[tid, "lastTradeSource"] = "carried(no rows today)"
        if str(k.at[tid, "status"]) not in ("confirmed",):
            k.at[tid, "status"] = "provisional"
            k.at[tid, "endReason"] = "data_gap"
        k.at[tid, "evidence"] = (str(k.at[tid, "evidence"]) + " | daily: no price rows left (vendor removed) -> previous dates carried")[:600]
        n += 1
    out = k.reset_index()
    for c in ("lastTradeDate", "ciqLastCloseDate", "miLastCloseDate", "firstTradeDate", "firstPriceDate"):
        out[c] = pd.to_datetime(out[c]).astype("datetime64[ns]")
    return out, n


END_INPUT_COLS = ("ciqLastCloseDate", "miLastCloseDate", "securityEndDate", "tradingItemStatusId")
END_CARRY_COLS = ("lastTradeDate", "lastTradeSource", "status", "endReason", "eventEndDate")


def carry_end_dates(lc_new: pd.DataFrame, lc_prev: pd.DataFrame | None, out_csv: Path | None = None) -> tuple[pd.DataFrame, dict]:
    """Snapshot -> daily transition guard (R-D2 P1-1a / §8, D3 path dependence): an ended (confirmed / provisional) id whose price and identity
    inputs did not change today (ciq/mi last close, securityEndDate, tradingItemStatusId) keeps the previous build's lastTradeDate /
    lastTradeSource / status / endReason / eventEndDate. The only thing that differs for such an id is the events file used as marker
    (the snapshot's T1 used the v1 events file, the daily uses the build's own events) - the fixed snapshot is not re-litigated by the
    daily; what the fresh classification would have said is listed (out_csv) for review. Active ids are always fresh."""
    if lc_prev is None or not len(lc_new):
        return lc_new, {"carried": 0}
    k = lc_new.set_index("tradingItemId")
    p = lc_prev.set_index("tradingItemId")
    common = k.index.intersection(p.index)
    ended_prev = p.loc[common, "status"].astype(str).isin(["confirmed", "provisional"])
    same = pd.Series(True, index=common)
    for c in END_INPUT_COLS:
        a, b = p.loc[common, c], k.loc[common, c]
        if str(a.dtype).startswith("datetime") or str(b.dtype).startswith("datetime"):
            a, b = pd.to_datetime(a), pd.to_datetime(b)
            same &= (a.isna() & b.isna()) | (a == b)
        else:
            same &= (a.isna() & b.isna()) | (a.astype(str) == b.astype(str))
    cand = common[(ended_prev & same).to_numpy()]
    rows = []
    n = 0
    for tid in cand:
        changed = []
        for c in END_CARRY_COLS:
            x, y = p.at[tid, c], k.at[tid, c]
            eq = (pd.isna(x) and pd.isna(y)) or (pd.notna(x) and pd.notna(y) and (str(x) == str(y) or (isinstance(x, pd.Timestamp) and isinstance(y, pd.Timestamp) and x == y)))
            if not eq:
                changed.append(f"{c}:{y}->{x}")
        if changed:
            rows.append({"tradingItemId": int(tid), "kept": ";".join(changed)[:300]})
            for c in END_CARRY_COLS:
                k.at[tid, c] = p.at[tid, c]
            k.at[tid, "evidence"] = (str(k.at[tid, "evidence"]) + " | daily: end dates carried from the previous build (inputs unchanged; marker source differs)")[:600]
            n += 1
    if out_csv is not None:
        pd.DataFrame(rows, columns=["tradingItemId", "kept"]).to_csv(out_csv, index=False)
    out = k.reset_index()
    for c in ("lastTradeDate", "eventEndDate"):
        out[c] = pd.to_datetime(out[c]).astype("datetime64[ns]")
    return out, {"carried": n, "candidates": int(len(cand))}


def carry_first_dates(lc_new: pd.DataFrame, lc_prev: pd.DataFrame, lineage: pd.DataFrame, delta_ids: set, override_ids: set, new_ids) -> tuple[pd.DataFrame, dict]:
    """P1-1: the snapshot's firstTradeDate / firstPriceDate / firstTradeSource are carried for every id whose inputs did not change today
    (ciq first close unchanged, not a delta id, not an override id, its lineage old id(s) lastTradeDate unchanged, not a new id); the T1
    lift (apply_lineage_first_dates) and the R3 patch are then re-evaluated only where inputs changed. Returns (lifecycle, info)."""
    k = lc_new.set_index("tradingItemId")
    p = lc_prev.set_index("tradingItemId")
    common = k.index.intersection(p.index)
    olds = {}
    for r in lineage[lineage.relation.isin(["rename", "relist"])].itertuples(index=False):
        olds.setdefault(int(r.newId), set()).add(int(r.oldId))
    skip = set(int(i) for i in delta_ids) | set(int(i) for i in (new_ids or []))          # an override sets the id's END, not its first date
    carried, recomputed = 0, []
    for tid in common:
        t = int(tid)
        if t in skip:
            recomputed.append((t, "delta/new"))
            continue
        fp_new, fp_prev = k.at[tid, "firstPriceDate"], p.at[tid, "firstPriceDate"]
        if not ((pd.isna(fp_new) and pd.isna(fp_prev)) or (pd.notna(fp_new) and pd.notna(fp_prev) and pd.Timestamp(fp_new) == pd.Timestamp(fp_prev))):
            recomputed.append((t, "firstPriceDate changed"))
            continue
        old_changed = False
        for o in olds.get(t, ()):
            if o in k.index and o in p.index:
                lo, lp = k.at[o, "lastTradeDate"], p.at[o, "lastTradeDate"]
                if not ((pd.isna(lo) and pd.isna(lp)) or (pd.notna(lo) and pd.notna(lp) and pd.Timestamp(lo) == pd.Timestamp(lp))):
                    old_changed = True
            elif o in k.index or o in p.index:
                old_changed = True
        if old_changed:
            recomputed.append((t, "old id lastTradeDate changed"))
            continue
        if not ((pd.isna(k.at[tid, "firstTradeDate"]) and pd.isna(p.at[tid, "firstTradeDate"])) or (pd.notna(k.at[tid, "firstTradeDate"]) and pd.notna(p.at[tid, "firstTradeDate"])
                                                                                                        and pd.Timestamp(k.at[tid, "firstTradeDate"]) == pd.Timestamp(p.at[tid, "firstTradeDate"]))) \
                or str(k.at[tid, "firstTradeSource"]) != str(p.at[tid, "firstTradeSource"]):
            k.at[tid, "firstTradeDate"] = p.at[tid, "firstTradeDate"]
            k.at[tid, "firstTradeSource"] = p.at[tid, "firstTradeSource"]
            carried += 1
    out = k.reset_index()
    out["firstTradeDate"] = pd.to_datetime(out.firstTradeDate).astype("datetime64[ns]")
    return out, {"carried_first_dates": carried, "recomputed_ids": len(recomputed), "recomputed_reasons": pd.Series([r[1] for r in recomputed]).value_counts().to_dict() if recomputed else {}}


def double_leg_check(ledger: pd.DataFrame, lineage: pd.DataFrame, lifecycle: pd.DataFrame, cal_days: np.ndarray, tol_days: int = 3) -> dict:
    """P0-2 (3): the same distribution (child, ratio) must not be booked on the old id AND the linked new id within a few bars around the
    transition (a settlement-bar alignment moving a keeper leg onto the link bar next to the stitched copy would count it twice)."""
    legs = ledger[ledger.legType.isin(["stock", "rights"]) & ledger.childTradingItemId.notna()]
    if not len(legs) or not len(lineage):
        return {"pass": True, "pairs_checked": 0, "double_legs": 0, "examples": []}
    legs = legs.assign(child=pd.to_numeric(legs.childTradingItemId).astype("int64"), ex=pd.to_datetime(legs.exDate))
    by_id = {t: g for t, g in legs.groupby(legs.tradingItemId.astype(int))}
    ex, n = [], 0
    pairs = lineage[lineage.relation.isin(["rename", "relist"])]
    for r in pairs.itertuples(index=False):
        old, new = int(r.oldId), int(r.newId)
        if old not in by_id or new not in by_id:
            continue
        a, b = by_id[old], by_id[new]
        for ra_ in a.itertuples(index=False):
            m = b[(b.child == ra_.child) & (np.abs(b.childRatio.astype(float) - float(ra_.childRatio)) <= 1e-9 * max(1.0, abs(float(ra_.childRatio)))) & ((b.ex - ra_.ex).abs() <= pd.Timedelta(days=tol_days)) & (b.ex != ra_.ex)]
            m = m[m.source.astype(str) != "stitched_copy"] if "source" in m.columns else m
            if len(m) and str(ra_.source) != "stitched_copy":
                n += 1
                if len(ex) < 20:
                    ex.append({"oldId": old, "newId": new, "child": int(ra_.child), "ratio": float(ra_.childRatio), "exDateOld": str(ra_.ex.date()), "exDateNew": str(m.ex.iloc[0].date()),
                               "eventIdOld": ra_.eventId, "eventIdNew": m.eventId.iloc[0]})
    return {"pass": n == 0, "pairs_checked": int(len(pairs)), "double_legs": n, "examples": ex}


def returns_outliers(old: dict, new: dict, prev_asof: pd.Timestamp, max_abs: float = 50.0, new_ids=None) -> dict:
    """G-RET-OUTLIER, relative to the previous build (the snapshot already holds 7,314 |ret|>50 / 49,358 >4x OTC tick-floor cells, retFlag 0):
    (a) blocking: a cell of an EXISTING id dated <= the previous asOf whose |log(1+ret)| > log 4 today but was not an outlier in the previous
    build (a historical return that became a >4x / <-75% move can only be a build inconsistency, P0-1 class);
    (b) reported: new-date cells (or cells of new ids) with |log(1+ret)| > log 4, and among them |ret| > max_abs (tick-floor names; a held one is
    an exposure hit)."""
    pa_day = int(np.datetime64(pd.Timestamp(prev_asof), "D").astype(np.int64))
    with np.errstate(invalid="ignore", divide="ignore"):
        lo = np.abs(np.log1p(old["ret"]))
        ln_ = np.abs(np.log1p(new["ret"]))
    out_old = old["key"][np.isfinite(lo) & (lo > LOG4)]
    m_new = np.isfinite(ln_) & (ln_ > LOG4)
    keys_new = new["key"][m_new]
    days = rr.key_days(keys_new)
    ids = rr.key_ids(keys_new)
    is_new_id = np.isin(ids, np.array(sorted(int(i) for i in (new_ids or [])), dtype=np.int64)) if new_ids else np.zeros(len(keys_new), dtype=bool)
    hist = keys_new[(days <= pa_day) & ~is_new_id]
    hist_new = hist[~np.isin(hist, out_old)]
    recent = keys_new[(days > pa_day) | is_new_id]
    pos_r = np.searchsorted(new["key"], recent)
    big_recent = recent[np.abs(new["ret"][pos_r]) > max_abs] if len(recent) else recent

    def rows(keys, lim=20):
        pos = np.searchsorted(new["key"], keys)
        return [{"tradingItemId": int(rr.key_ids(np.array([k]))[0]), "date": str(np.datetime64(int(rr.key_days(np.array([k]))[0]), "D")), "ret": float(new["ret"][p]), "retFlag": int(new["flag"][p])}
                for k, p in list(zip(keys, pos))[:lim]]
    return {"pass": len(hist_new) == 0, "new_historical_outliers": int(len(hist_new)), "max_abs": max_abs, "new_date_outliers": int(len(recent)),
            "new_date_abs_gt_max": int(len(big_recent)), "outliers_total": int(m_new.sum()), "outliers_prev_total": int(len(out_old)),
            "examples": rows(hist_new), "new_date_examples": rows(big_recent) + rows(recent[~np.isin(recent, big_recent)], 10)}
