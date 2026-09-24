#!/home/jgo/usr/venv-3.11/bin/python
"""cache_v21/returns_v2_qa.py - R2 gates for derived/returns_total.parquet v2 (rc2 BRIEF section 3-2).

Gates
  G-CASH    rows with no ledger event in (prevDate, date] and no lineage link: ret == close_t/close_prev - 1 bit-identical
            (np.array_equal on float64; close is the stored float32 promoted to float64, the generator's input).
  G-SPLIT   rows with a quantity leg (split / stock_dividend) in (prevDate, date]: prod(parentFactor) x close_t/close_prev - 1 == split_adj return
            (close_t/sf_t)/(close_prev/sf_prev) - 1 within 1e-12 (relative to max(1, |ret|)). Rows where the ledger split product differs from the
            vendor split ratio are listed (gsplit_mismatch.csv) and pass only if the ledger row carries repairOf.
  G-ORACLE  section 3-2 cases, expected values recomputed from base_data raw closes (+ the section 2 raw numbers for
            DD/HON): DD 2025-11-03, HON 2026-06-29, COST 2020-12-01, EBAY 2015-07-20, CAR 2006-08-01, HPQ 2015-11-02,
            XRX 2017-01-03 (both lines), LCAP.A 2008-03-04, T 2002-11-19, TYC 314828302 2007-07-02 & 2012-10-01,
            PBCT 215291786 2000-2007 (every cash day = cash formula with the ciqdividend amount; vendor x2.98 scale gone).
  G-VENDOR  every (id, date) with |ret_v2 - ret_rc1| > 1e-6 -> qa/returns_v2/vendor_diff.parquet with class in
            {cash_formula, distribution_formula, repaired_factor, estimate, unresolved, unexplained} + reason; unexplained
            must be 0. Rows v2 has but rc1 lacks (lineage-linked first rows, lifecycle-masked tails) are counted apart.
  G-NAN     NaN ret only on an id's first observed row; ids with a NaN first row have no resolvable rename/relist link.
  G-LINK    (rework F3) every lineage-linked first row recomputed independently (plain per-link loop: old id's last close
            before the new id's first close, old-id ledger events in the gap on one old share, conversion = old quantity
            legs' product else lineage ratio, new-id gap events on the converted shares minus duplicates, plausibility
            flags) and compared with the v2 file (ret 1e-12 relative, retFlag, prevDate); the v2 linked-row set must
            equal the recomputed set. lineage_links.csv lists every candidate with state, suspect reason, oldSameDayRet.
  extra     ledger_gap_candidates.csv (rework F7: vendor dividend steps with no ledger event, for R1),
            settlement_like_cash_rows.csv (rework F6: cash >= 0.9 x P_prev with the position still worth >= 50%, for V2).

CLI:
  returns_v2_qa.py --build <rc1 build dir> --v2 <returns_total v2> --ledger <ca_ledger.parquet> --out <qa dir>
                   [--lineage <lineage.parquet>] [--rc1 <returns_total rc1>] [--base-data <path>] [--self-test]
  --lineage must be the file the generator used (its info records lineage_sha256); without it the ledger's rename/relist
  rows are the link source, as in the generator.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from prices_factors import FactorLookup  # noqa: E402
import prices_returns as prm  # noqa: E402
from prices_returns import (APPLY_TYPES, EXCLUDED_SOURCES, FLAG_NAMES, LINK_RET_SUSPECT, LINK_TYPES, MAX_CHAIN_GAP, RATIO_CONFLICT_TOL, VALUE_LEGS, Ledger, _days, _keys,  # noqa: E402
                            applied_mask, load_ledger, load_ledger_info, leg_flags, resolve_links)

log = logging.getLogger("cache_v21.returns_v2_qa")
DIFF_TOL = 1e-6
SPLIT_TOL = 1e-12
CASH_REL_TOL = 0.005          # vendor-implied D within 0.5% (+1e-4 abs) of the ledger cash -> pure formula difference
CLASSES = ["cash_formula", "distribution_formula", "repaired_factor", "repaired_factor(csv)", "repaired_factor(csv,unverified)", "repaired_factor(csv,unchecked)",
           "estimate", "unresolved", "price_override", "link_row", "unexplained"]


def load_split_overrides(stage: Path | None) -> pd.DataFrame:
    """rc2f stage (rc2f_apply output): applied split overrides -> boundary days per id (R-X0 2nd review P1-7). Columns tradingItemId, day, overrideId,
    action, status. drop/set_factor/add/confirm: exDate; move_date: exDate (removed) and newExDate (new boundary)."""
    cols = ["tradingItemId", "day", "overrideId", "action", "status"]
    if stage is None:
        return pd.DataFrame(columns=cols)
    stage = Path(stage)
    log_p = stage / "rc2f_apply_log.csv"
    if not log_p.exists():
        return pd.DataFrame(columns=cols)
    lg = pd.read_csv(log_p, dtype=str, keep_default_na=False)
    lg = lg[(lg.kind == "split") & lg.status.isin(["applied", "applied_unverified", "applied_unchecked"])]
    if not len(lg):
        return pd.DataFrame(columns=cols)
    new_ex = {}
    for p in sorted((stage / "overrides").glob("*split_overrides*.csv")) if (stage / "overrides").exists() else []:
        o = pd.read_csv(p, dtype=str, keep_default_na=False)
        for r in o.itertuples(index=False):
            if str(getattr(r, "newExDate", "")).strip():
                new_ex[r.overrideId] = pd.Timestamp(r.newExDate.strip())
    rows = []
    for r in lg.itertuples(index=False):
        try:
            tid, ex = r.target.split("@"); tid = int(tid); ex = pd.Timestamp(ex)
        except (ValueError, AttributeError):
            continue
        rows.append((tid, _days(np.array([ex], dtype="datetime64[ns]"))[0], r.overrideId, r.action, r.status))
        if r.action == "move_date" and r.overrideId in new_ex:
            rows.append((tid, _days(np.array([new_ex[r.overrideId]], dtype="datetime64[ns]"))[0], r.overrideId, r.action, r.status))
    return pd.DataFrame(rows, columns=cols)


def split_override_hits(ovr: pd.DataFrame, ids: np.ndarray, prev_days: np.ndarray, days: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per cell: overrideIds whose boundary day b satisfies prevDay < b <= day on the same id ('' if none) and the worst status."""
    n = len(ids)
    oid = np.full(n, "", dtype=object); st = np.full(n, "", dtype=object)
    if ovr is None or not len(ovr):
        return oid, st
    by = {t: g for t, g in ovr.groupby("tradingItemId")}
    cand = np.flatnonzero(np.isin(ids, list(by.keys())))
    for i in cand:
        g = by[int(ids[i])]
        m = (g.day.to_numpy() > int(prev_days[i])) & (g.day.to_numpy() <= int(days[i]))
        if m.any():
            oid[i] = ";".join(dict.fromkeys(g.overrideId.to_numpy()[m]))
            sts = g.status.to_numpy()[m]
            st[i] = "applied_unverified" if (sts == "applied_unverified").any() else ("applied_unchecked" if (sts == "applied_unchecked").any() else "applied")
    return oid, st
QF_PRICE_OVERRIDE = 8          # base_data.qualityFlag: rc2f 수동 가격 교정(rc2f_apply.py) — 그 날·전일 가격이 바뀐 셀은 벤더(rc1) 수익률과 다른 게 정상
QF_PRICE_OVERRIDE_EST = 32     # 추정 가격 교정(price_overrides isEstimate true): 같은 분류, returns 행은 retFlag >= 3

# oracle cases: (label, tradingItemId, date, kind, spec)
#   kind 'raw'   : expected from the section 2 raw numbers (value)
#   kind 'cash'  : expected = (close_t + cash) / close_prev - 1 with cash from the spec
#   kind 'child' : expected = (pf x close_t + sum ratio_i x child_i close_t) / close_prev - 1
#   kind 'unres' : expected retFlag 4 unless the ledger resolved the child (then 'child' spec applies)
ORACLE = [
    ("DD 2025-11-03 (Q 0.5, section 2 raw 81.65 -> 34.69, Q 97.00)", 383332720, "2025-11-03", "raw", {"value": (34.69 + 0.5 * 97.00) / 81.65 - 1, "display": "+1.89%"}),
    ("HON 2026-06-29 (1:2 reverse + HONA 0.5, section 2 raw 232.21 -> 227.80, HONA 220.19)", 2618950, "2026-06-29", "raw", {"value": (0.5 * 227.80 + 0.5 * 220.19) / 232.21 - 1, "display": "-3.5%"}),
    ("COST 2020-12-01 special dividend 10 cash-style", 2603626, "2020-12-01", "cash", {"cash": 10.0}),
    ("EBAY 2015-07-20 PYPL 1.0 x child close", 2608344, "2015-07-20", "child", {"pf": 1.0, "children": [(306093931, 1.0)]}),
    ("CAR 2006-08-01 Realogy 0.25 + Wyndham 0.20 x child closes", 28942786, "2006-08-01", "child", {"pf": 1.0, "children": [(28067777, 0.25), (28067782, 0.20)]}),
    ("HPQ 2015-11-02 HPE 1.0 x child close", 2618412, "2015-11-02", "child", {"pf": 1.0, "children": [(313064514, 1.0)]}),
    ("XRX 2665260 2017-01-03 CNDT 0.2 x child close", 2665260, "2017-01-03", "child", {"pf": 1.0, "children": [(412909487, 0.2)]}),
    ("XRX 610592433 2017-01-03 CNDT 0.2 x child close", 610592433, "2017-01-03", "child", {"pf": 1.0, "children": [(412909487, 0.2)]}),
    ("LCAP.A 2008-03-04 LMDIA 4.0 x child close (child priced only if the ledger resolves it)", 27059655, "2008-03-04", "unres", {"pf": 1.0, "ratio": 4.0}),
    ("T 2002-11-19 1:5 reverse + CMCSA 0.3235 per pre-split share", 2586324, "2002-11-19", "child", {"pf": 0.2, "children": [(2601563, 0.3235)]}),
    ("TYC 314828302 2007-07-02 1:4 reverse + COV 0.25 + TEL 0.25", 314828302, "2007-07-02", "child", {"pf": 0.25, "children": [(34969538, 0.25), (1876975607, 0.25)]}),
    ("TYC 314828302 2012-10-01 ADT 0.5 + PNR 0.239943 (8-K Ex.99.1; rc2f X2-266)", 314828302, "2012-10-01", "child", {"pf": 1.0, "children": [(217292918, 0.5), (2638398, 0.239943)]}),
]
PBCT_OLD = 215291786


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------
class Membership:
    """universe/membership.parquet -> (id, date) in Top500 / Top1000."""

    def __init__(self, path: Path | None):
        self.tables = {}
        if path is None or not Path(path).exists():
            return
        m = pd.read_parquet(path)
        for u in ("Top500", "Top1000"):
            x = m[m.universe == u].sort_values(["tradingItemId", "fromDate"])
            self.tables[u] = (x.tradingItemId.to_numpy(np.int64), _days(x.fromDate.to_numpy()), _days(x.toDate.to_numpy()))

    def member(self, u: str, ids, days) -> np.ndarray:
        if u not in self.tables:
            return np.zeros(len(np.asarray(ids)), dtype=bool)
        mid, mfrom, mto = self.tables[u]
        if len(mid) == 0:
            return np.zeros(len(np.asarray(ids)), dtype=bool)
        keys = _keys(mid, mfrom)
        q = _keys(ids, days)
        pos = np.searchsorted(keys, q, side="right") - 1
        ok = pos >= 0
        p = np.maximum(pos, 0)
        ok &= (mid[p] == np.asarray(ids, dtype=np.int64)) & (np.asarray(days, dtype=np.int64) <= mto[p])
        return ok


def ledger_interval_summary(ledger_df: pd.DataFrame, ids: np.ndarray, prev_days: np.ndarray, days: np.ndarray, lifecycle: pd.DataFrame | None = None,
                            legacy: bool = False) -> pd.DataFrame:
    """For query rows (id, prevDay, day) summarise the applied ledger legs with prevDay < exDay <= day.
    Returns a frame aligned to the queries: n_legs, n_cash, n_value, n_split, cash_sum (per old share, nominal),
    child_sum, pf_prod, max_flag, any_repair, types, statuses."""
    a = ledger_df[applied_mask(ledger_df, lifecycle=lifecycle, legacy=legacy)].copy()   # same row set as the generator (rework F2; rc2f settlement cash rule)
    a["tradingItemId"] = a.tradingItemId.astype("int64")
    a["day"] = _days(pd.to_datetime(a.exDate).to_numpy())
    a = a.sort_values(["tradingItemId", "day"], kind="stable").reset_index(drop=True)
    a["flag"] = leg_flags(a)
    a["src"] = (a["_effSource"] if "_effSource" in a else a.source).astype("object").fillna("") if "source" in a else ""   # keeper source for stitched copies
    lk = _keys(a.tradingItemId.to_numpy(np.int64), a.day.to_numpy())
    q_lo = _keys(ids, prev_days)              # exclusive
    q_hi = _keys(ids, days)                   # inclusive
    lo = np.searchsorted(lk, q_lo, side="right")
    hi = np.searchsorted(lk, q_hi, side="right")
    n = len(ids)
    out = {"n_legs": hi - lo, "n_cash": np.zeros(n, int), "n_value": np.zeros(n, int), "n_split": np.zeros(n, int),
           "cash_sum": np.zeros(n), "child_sum": np.zeros(n), "pf_prod": np.ones(n), "max_flag": np.zeros(n, np.int8),
           "any_repair": np.zeros(n, bool), "types": np.full(n, "", dtype=object), "statuses": np.full(n, "", dtype=object),
           "legs": np.full(n, "", dtype=object), "n_cash_ciqdividend": np.zeros(n, int), "n_cash_manual": np.zeros(n, int), "sources": np.full(n, "", dtype=object),
           "n_stitched": np.zeros(n, int)}
    leg = a.legType.astype(str).to_numpy(); et = a.eventType.astype(str).to_numpy(); stt = a.status.astype(str).to_numpy(); src = a.src.astype(str).to_numpy()
    sti = a["_stitched"].fillna(False).to_numpy(dtype=bool) if "_stitched" in a else np.zeros(len(a), bool)
    cps = a.cashPerShare.to_numpy(np.float64); cval = a.childValuePerOldShare.to_numpy(np.float64); pf = a.parentFactor.to_numpy(np.float64)
    rep = a.repairOf.notna().to_numpy() if "repairOf" in a else np.zeros(len(a), bool)
    fl = a.flag.to_numpy()
    for i in np.flatnonzero(hi > lo):
        s = slice(lo[i], hi[i])
        out["n_cash"][i] = int((leg[s] == "cash").sum()); out["n_value"][i] = int(np.isin(leg[s], ["stock", "rights"]).sum())
        out["n_cash_ciqdividend"][i] = int(((leg[s] == "cash") & np.char.startswith(src[s].astype(str), "ciqdividend") & np.isin(et[s], ["cash_dividend", "special_dividend"])).sum())   # incl. ciqdividend(aux)
        out["n_cash_manual"][i] = int(((leg[s] == "cash") & (src[s] == "manual_override")).sum())
        out["n_stitched"][i] = int(sti[s].sum())
        out["sources"][i] = "+".join(src[s])
        out["n_split"][i] = int((leg[s] == "quantity").sum())          # split + stock_dividend quantity legs
        out["cash_sum"][i] = float(np.nansum(cps[s])); out["child_sum"][i] = float(np.nansum(cval[s])); out["pf_prod"][i] = float(np.prod(pf[s]))
        out["max_flag"][i] = int(fl[s].max()); out["any_repair"][i] = bool(rep[s].any())
        out["types"][i] = "+".join(et[s]); out["statuses"][i] = "+".join(stt[s])
        out["legs"][i] = "|".join(f"{e}:{l}:{'nan' if not np.isfinite(c) else round(c, 6)}:{'nan' if not np.isfinite(v) else round(v, 6)}:{p:g}:{t}"
                                  for e, l, c, v, p, t in zip(et[s], leg[s], cps[s], cval[s], pf[s], stt[s]))
    return pd.DataFrame(out)


# --------------------------------------------------------------------------------------
# main pass
# --------------------------------------------------------------------------------------
def run_gates(build: Path, v2: Path, ledger_path: Path, out: Path, rc1: Path | None = None, base_data: Path | None = None,
              lineage: Path | None = None, stitched_copies: Path | bool | None = None, lifecycle: Path | None = None, legacy_links: bool = False,
              vendor_factors: Path | None = None, rc2f_stage: Path | None = None) -> dict:
    """vendor_factors: the VENDOR split factors (rc2 original factors/split.parquet, before rc2f split overrides) used as the reference for G-SPLIT and
    G-VENDOR — with the build's corrected factors a wrong drop/move_date would hide behind 'no vendor step' (R-X0 2nd review P1-7). rc2f_stage: the
    rc2f_apply output; its applied split overrides classify the cells at corrected boundaries as repaired_factor(csv[,unverified]) and list them."""
    t0 = time.time()
    build = Path(build); out = Path(out); out.mkdir(parents=True, exist_ok=True)
    for stale in ("gsplit_mismatch.csv", "gnan_not_first_row.csv", "vendor_diff.parquet", "lineage_links.csv", "glink_mismatch.csv",
                  "ledger_gap_candidates.csv", "settlement_like_cash_rows.csv", "split_override_cells.csv", "gsplit_csv_override_rows.csv"):
        (out / stale).unlink(missing_ok=True)
    base = Path(base_data) if base_data else build / "base_data.parquet"
    rc1 = Path(rc1) if rc1 else build / "derived" / "returns_total.parquet"
    ldf, stitched = load_ledger_info(ledger_path, stitched_copies)
    lifecycle_df = None
    if lifecycle is None and (build / "master" / "lifecycle.parquet").exists():
        lifecycle = build / "master" / "lifecycle.parquet"
    if lifecycle is not None and not legacy_links:
        lifecycle_df = pd.read_parquet(lifecycle, columns=["tradingItemId", "lastTradeDate"])
    ledger = Ledger(ldf, lifecycle=lifecycle_df, legacy_rules=legacy_links)
    lineage_df = pd.read_parquet(lineage) if lineage is not None else None
    if lineage_df is not None:
        ledger.use_lineage(lineage_df, legacy=legacy_links)
    sf_lookup = FactorLookup(pd.read_parquet(Path(vendor_factors) if vendor_factors else build / "factors" / "split.parquet"))
    df_lookup = FactorLookup(pd.read_parquet(build / "factors" / "dividend.parquet"))
    split_ovr = load_split_overrides(rc2f_stage)
    split_csv_rows = []
    memb = Membership(build / "universe" / "membership.parquet")
    pf_base = pq.ParquetFile(base); pf_v2 = pq.ParquetFile(v2); pf_rc1 = pq.ParquetFile(rc1)
    if pf_base.num_row_groups != pf_v2.num_row_groups:
        raise RuntimeError(f"row groups differ: base {pf_base.num_row_groups} vs v2 {pf_v2.num_row_groups}")
    # rc1 keyed arrays (47.99M rows: ~0.8 GB) for the vendor comparison
    t = pq.read_table(rc1, columns=["date", "tradingItemId", "ret"])
    rc1_keys = _keys(t["tradingItemId"].to_numpy(), _days(t["date"].to_numpy().astype("datetime64[ns]")))
    rc1_ret = t["ret"].to_numpy(zero_copy_only=False).astype(np.float64)
    del t
    if not (np.diff(rc1_keys) > 0).all():
        o = np.argsort(rc1_keys, kind="stable"); rc1_keys = rc1_keys[o]; rc1_ret = rc1_ret[o]
    G = {"G-CASH": {"rows_checked": 0, "mismatch": 0}, "G-SPLIT": {"rows_checked": 0, "fail": 0, "ledger_vs_vendor_mismatch": 0, "csv_override_rows": 0,
                                                                     "vendor_factors": str(vendor_factors) if vendor_factors else "build", "split_overrides": int(len(split_ovr))},
         "G-NAN": {"nan_rows": 0, "nan_not_first_row": 0}, "rows": 0, "event_rows": 0, "linked_rows": 0,
         "rc1_missing_rows": 0, "rc1_nan_v2_value": 0, "v2_nan_rc1_value": 0, "retFlag": {k: 0 for k in FLAG_NAMES.values()}}
    diffs = []; split_bad = []; nan_first_ids = []; nan_rows_not_first = []; v2_linked = []
    # independent link plan (newId, linkDate) computed BEFORE reading v2: marks the rows the generator must have linked (rc2f: a link row can be a
    # mid-series row after a when-issued overlap, indistinguishable from a plain row by prevDate alone when both ids printed on the old id's last day)
    lk_plan = link_recompute(base, ldf, lineage_df, pd.DataFrame(), memb, lifecycle_df=lifecycle_df, legacy=legacy_links)
    plan_linked = lk_plan[lk_plan.state == "linked"] if len(lk_plan) else lk_plan
    plan_keys = np.sort(_keys(plan_linked.newId.to_numpy(np.int64), _days(pd.to_datetime(plan_linked.linkDate).to_numpy().astype("datetime64[ns]")))) if len(plan_linked) else np.zeros(0, np.int64)
    has_qf = "qualityFlag" in pf_base.schema_arrow.names
    for rg in range(pf_base.num_row_groups):
        tb = pf_base.read_row_group(rg, columns=["date", "tradingItemId", "close"] + (["qualityFlag"] if has_qf else []))
        close = tb["close"].to_numpy(zero_copy_only=False)
        keep = np.isfinite(close)
        ids = tb["tradingItemId"].to_numpy()[keep]; dates = tb["date"].to_numpy().astype("datetime64[ns]")[keep]; close = close[keep].astype(np.float64)
        qf = tb["qualityFlag"].to_numpy()[keep] if has_qf else np.zeros(len(ids), np.int8)
        tv = pf_v2.read_row_group(rg)
        vid = tv["tradingItemId"].to_numpy(); vdates = tv["date"].to_numpy().astype("datetime64[ns]")
        if len(vid) != len(ids) or not (np.array_equal(vid, ids) and np.array_equal(vdates, dates)):
            raise RuntimeError(f"row group {rg}: v2 rows are not the close-notna rows of base_data in order")
        ret = tv["ret"].to_numpy(zero_copy_only=False).astype(np.float64)
        flag = tv["retFlag"].to_numpy().astype(np.int8)
        prev_v2 = tv["prevDate"].to_numpy().astype("datetime64[ns]")
        n = len(ids)
        days = _days(dates)
        same = np.r_[False, ids[1:] == ids[:-1]]
        is_last = np.r_[ids[1:] != ids[:-1], True]
        close_prev = np.r_[np.nan, close[:-1]]; close_prev[~same] = np.nan
        prev_days = np.r_[-1, days[:-1]]; prev_days[~same] = -1
        qf_prev = np.r_[0, qf[:-1]]; qf_prev[~same] = 0
        price_ovr = np.isin(qf, (QF_PRICE_OVERRIDE, QF_PRICE_OVERRIDE_EST)) | np.isin(qf_prev, (QF_PRICE_OVERRIDE, QF_PRICE_OVERRIDE_EST))
        # linked row: a first row with a prevDate (generator-declared) or a row of the independent link plan (newId, linkDate)
        pv = pd.isna(prev_v2)
        linked = (~pv & ~same) | (np.isin(_keys(ids, days), plan_keys) & ~pv)
        # events in (prevDate, date] from the ledger (independently of the generator's bookkeeping)
        q_prev = prev_days.copy()
        if linked.any():
            q_prev[linked] = _days(prev_v2[linked])
        lo = np.searchsorted(ledger.keys, _keys(ids, q_prev), side="right")
        hi = np.searchsorted(ledger.keys, _keys(ids, days), side="right")
        has_ev = (hi > lo) & (same | linked)
        plain = close / close_prev - 1.0
        # ---- G-CASH
        m = ~has_ev & ~linked
        G["G-CASH"]["rows_checked"] += int(m.sum())
        eq = (ret[m] == plain[m]) | (np.isnan(ret[m]) & np.isnan(plain[m]))
        G["G-CASH"]["mismatch"] += int((~eq).sum())
        # ---- G-NAN
        nanm = np.isnan(ret)
        G["G-NAN"]["nan_rows"] += int(nanm.sum())
        bad = nanm & same
        G["G-NAN"]["nan_not_first_row"] += int(bad.sum())
        if bad.any():
            nan_rows_not_first.extend(zip(ids[bad].tolist(), dates[bad].astype("datetime64[D]").astype(str).tolist()))
        nan_first_ids.extend(np.unique(ids[nanm & ~same]).tolist())
        # ---- G-SPLIT (rows with a split leg in the interval)
        idx_ev = np.flatnonzero(has_ev)
        if len(idx_ev):
            summ = ledger_interval_summary(ldf, ids[idx_ev], q_prev[idx_ev], days[idx_ev], lifecycle=lifecycle_df, legacy=legacy_links)
            sp = summ.n_split.to_numpy() > 0
            if sp.any():
                ii = idx_ev[sp]
                sf_t = sf_lookup(ids[ii], dates[ii]); sf_p = sf_lookup(ids[ii], q_prev[ii].astype("datetime64[D]").astype("datetime64[ns]"))
                vendor = (close[ii] / sf_t) / (close_prev[ii] / sf_p) - 1.0
                ours = summ.pf_prod.to_numpy()[sp] * close[ii] / close_prev[ii] - 1.0
                d = np.abs(ours - vendor) / np.maximum(1.0, np.abs(vendor))          # relative for huge (artifact) ratios
                G["G-SPLIT"]["rows_checked"] += int(len(ii))
                fail = d > SPLIT_TOL
                ratio_mismatch = np.abs(summ.pf_prod.to_numpy()[sp] - sf_p / sf_t) > 1e-9 * np.maximum(1.0, sf_p / sf_t)
                # rc2f: rows whose interval holds a corrected split boundary differ from the VENDOR factors by construction -> listed, not failed
                so_oid, so_st = split_override_hits(split_ovr, ids[ii], q_prev[ii], days[ii])
                is_csv = so_oid != ""
                for k in np.flatnonzero(is_csv & (fail | ratio_mismatch)):
                    split_csv_rows.append({"tradingItemId": int(ids[ii[k]]), "date": str(dates[ii[k]].astype("datetime64[D]")), "prevDate": str(np.datetime64(int(q_prev[ii[k]]), "D")),
                                           "ledger_pf_prod": float(summ.pf_prod.to_numpy()[sp][k]), "vendor_sf_ratio": float(sf_p[k] / sf_t[k]), "ret_v2": float(ours[k]),
                                           "ret_vendor_factors": float(vendor[k]), "overrideId": so_oid[k], "status": so_st[k], "legs": summ.legs.to_numpy()[sp][k]})
                fail &= ~is_csv; ratio_mismatch &= ~is_csv
                G["G-SPLIT"]["fail"] += int(fail.sum())
                G["G-SPLIT"]["ledger_vs_vendor_mismatch"] += int(ratio_mismatch.sum())
                G["G-SPLIT"]["csv_override_rows"] += int(is_csv.sum())
                for k in np.flatnonzero(fail | ratio_mismatch):
                    split_bad.append({"tradingItemId": int(ids[ii[k]]), "date": str(dates[ii[k]].astype("datetime64[D]")), "prevDate": str(np.datetime64(int(q_prev[ii[k]]), "D")),
                                      "ledger_pf_prod": float(summ.pf_prod.to_numpy()[sp][k]), "vendor_sf_ratio": float(sf_p[k] / sf_t[k]), "abs_diff_ret": float(d[k]),
                                      "any_repair": bool(summ.any_repair.to_numpy()[sp][k]), "legs": summ.legs.to_numpy()[sp][k]})
        # ---- G-VENDOR: align with rc1
        k = _keys(ids, days)
        pos = np.searchsorted(rc1_keys, k)
        pc = np.minimum(pos, len(rc1_keys) - 1)
        found = rc1_keys[pc] == k
        G["rc1_missing_rows"] += int((~found).sum())
        r1 = np.full(n, np.nan); r1[found] = rc1_ret[pc[found]]
        G["rc1_nan_v2_value"] += int((found & np.isnan(r1) & ~np.isnan(ret)).sum())
        G["v2_nan_rc1_value"] += int((found & ~np.isnan(r1) & np.isnan(ret)).sum())
        dmask = found & np.isfinite(ret) & np.isfinite(r1) & (np.abs(ret - r1) > DIFF_TOL)
        if dmask.any():
            di = np.flatnonzero(dmask)
            summ = ledger_interval_summary(ldf, ids[di], q_prev[di], days[di], lifecycle=lifecycle_df, legacy=legacy_links)
            prev_dates = q_prev[di].astype("datetime64[D]").astype("datetime64[ns]")
            sf_t = sf_lookup(ids[di], dates[di]); sf_p = sf_lookup(ids[di], prev_dates)
            df_t = df_lookup(ids[di], dates[di]); df_p = df_lookup(ids[di], prev_dates)
            so_oid, so_st = split_override_hits(split_ovr, ids[di], q_prev[di], days[di])
            fr = pd.DataFrame({"tradingItemId": ids[di], "date": dates[di], "prevDate": prev_dates, "ret_v2": ret[di], "ret_rc1": r1[di],
                               "diff": ret[di] - r1[di], "retFlag": flag[di], "close_t": close[di], "close_prev": close_prev[di],
                               "sf_t": sf_t, "sf_prev": sf_p, "df_t": df_t, "df_prev": df_p, "linked": linked[di], "is_last_obs": is_last[di],
                               "price_override": price_ovr[di], "split_override": so_oid, "split_override_status": so_st})
            fr = pd.concat([fr, summ], axis=1)
            fr["Top500"] = memb.member("Top500", ids[di], days[di]); fr["Top1000"] = memb.member("Top1000", ids[di], days[di])
            diffs.append(fr)
        if linked.any():
            li = np.flatnonzero(linked)
            v2_linked.append(pd.DataFrame({"newId": ids[li], "date": dates[li], "prevDate_v2": prev_v2[li], "ret_v2": ret[li], "flag_v2": flag[li], "close_first": close[li]}))
        G["rows"] += n; G["event_rows"] += int(has_ev.sum()); G["linked_rows"] += int(linked.sum())
        bc = np.bincount(flag, minlength=5)
        for i, name in FLAG_NAMES.items():
            G["retFlag"][name] += int(bc[i])
        if (rg + 1) % 10 == 0:
            log.info("qa: %d/%d row groups (%.0fs)", rg + 1, pf_base.num_row_groups, time.time() - t0)
    del rc1_keys, rc1_ret
    # ---- classify vendor diffs
    vd = pd.concat(diffs, ignore_index=True) if diffs else pd.DataFrame()
    if len(vd):
        vd = classify_vendor_diff(vd)
        vd.to_parquet(out / "vendor_diff.parquet", index=False)
    cls_counts = vd["class"].value_counts().to_dict() if len(vd) else {}
    if len(vd):
        extra_lists(vd, build, memb, out)
        soc = vd[vd["class"].str.startswith("repaired_factor(csv")]
        if len(soc):      # R-X0 2nd review P1-7: every cell at a corrected split boundary, largest |diff| first (a wrong drop/move_date shows here)
            soc.assign(abs_diff=soc["diff"].abs()).sort_values("abs_diff", ascending=False).drop(columns=["abs_diff"]).to_csv(out / "split_override_cells.csv", index=False)
    if split_csv_rows:
        pd.DataFrame(split_csv_rows).to_csv(out / "gsplit_csv_override_rows.csv", index=False)
    G["G-VENDOR"] = {"diff_cells": int(len(vd)), "classes": {c: int(cls_counts.get(c, 0)) for c in CLASSES},
                     "unexplained": int(cls_counts.get("unexplained", 0)), "pass": int(cls_counts.get("unexplained", 0)) == 0,
                     "split_override_cells": {"cells": int(len(soc)) if len(vd) else 0,
                                              "by_override": soc.split_override.value_counts().to_dict() if len(vd) and len(soc) else {},
                                              "unverified": int((soc["class"] == "repaired_factor(csv,unverified)").sum()) if len(vd) and len(soc) else 0,
                                              "unchecked": int((soc["class"] == "repaired_factor(csv,unchecked)").sum()) if len(vd) and len(soc) else 0,
                                              "max_abs_diff": float(soc["diff"].abs().max()) if len(vd) and len(soc) else 0.0},
                     "by_type": vendor_diff_tables(vd) if len(vd) else {}}
    # ---- G-LINK (rework F3): independent recomputation of every linked first row; G-NAN: NaN first rows have no resolvable link
    v2l = pd.concat(v2_linked, ignore_index=True) if v2_linked else pd.DataFrame(columns=["newId", "date", "prevDate_v2", "ret_v2", "flag_v2", "close_first"])
    lk = _merge_v2_into_links(lk_plan, v2l)
    if len(lk):
        lk.to_csv(out / "lineage_links.csv", index=False)
    linked_lk = lk[lk.state == "linked"] if len(lk) else lk
    set_v2 = set(v2l.newId.astype(int).tolist()); set_rc = set(linked_lk.newId.astype(int).tolist()) if len(lk) else set()
    mism = linked_lk[~linked_lk.match] if len(linked_lk) else linked_lk
    if len(mism):
        mism.to_csv(out / "glink_mismatch.csv", index=False)
    G["G-LINK"] = {"links_recomputed": int(len(linked_lk)), "v2_linked_rows": int(len(v2l)), "set_equal": set_v2 == set_rc,
                   "only_v2": sorted(set_v2 - set_rc)[:10], "only_recomputed": sorted(set_rc - set_v2)[:10],
                   "value_or_flag_mismatch": int(len(mism)), "with_old_gap_events": int((linked_lk.gapN > 0).sum()) if len(linked_lk) else 0,
                   "ratio_conflict": int(linked_lk.ratioConflict.sum()) if len(linked_lk) else 0,
                   "suspect": linked_lk.suspect.value_counts().to_dict() if len(linked_lk) else {},
                   "pass": bool(set_v2 == set_rc and len(mism) == 0)}
    nan_ids = np.unique(np.array(nan_first_ids, dtype=np.int64))
    # after_old_last(_mirror) links put the link row after the old id's last trade -> the new id's first row is legitimately NaN
    first_row_links = set(linked_lk[~linked_lk.linkMode.astype(str).str.startswith("after_old_last")].newId.astype(int).tolist()) if len(linked_lk) and "linkMode" in linked_lk else set_rc
    nan_resolvable = sorted(int(i) for i in nan_ids if int(i) in first_row_links)
    G["G-NAN"].update({"ids_nan_first_row": int(len(nan_ids)), "link_rows": int(len(lk)),
                       "links_resolved": int(len(set_rc)), "links_unresolvable": int((lk.state != "linked").sum()) if len(lk) else 0,
                       "ids_nan_first_row_but_link_resolvable": len(nan_resolvable),
                       "pass": G["G-NAN"]["nan_not_first_row"] == 0 and len(nan_resolvable) == 0})
    G["G-NAN"]["nan_but_resolvable_ids_sample"] = nan_resolvable[:20]
    if nan_rows_not_first:
        pd.DataFrame(nan_rows_not_first, columns=["tradingItemId", "date"]).to_csv(out / "gnan_not_first_row.csv", index=False)
    G["G-CASH"]["pass"] = G["G-CASH"]["mismatch"] == 0
    G["G-SPLIT"]["pass"] = G["G-SPLIT"]["fail"] == 0 and all(b["any_repair"] for b in split_bad if abs(b["ledger_pf_prod"] - b["vendor_sf_ratio"]) > 1e-9)
    if split_bad:
        pd.DataFrame(split_bad).to_csv(out / "gsplit_mismatch.csv", index=False)
    # ---- G-ORACLE
    G["G-ORACLE"] = oracle_gate(build, base, v2, ldf, out, rc1=rc1)
    G["stitched_copies"] = stitched
    G["seconds"] = round(time.time() - t0, 1)
    G["pass"] = all(G[g]["pass"] for g in ("G-CASH", "G-SPLIT", "G-ORACLE", "G-VENDOR", "G-NAN", "G-LINK"))
    (out / "gates.json").write_text(json.dumps(G, indent=1, default=str))
    return G


def _link_candidates(ldf: pd.DataFrame, lineage_df: pd.DataFrame | None, legacy: bool = False) -> pd.DataFrame:
    """Same selection rules as the generator, written independently: (newId, oldId, ratio, ratioAssumed, chainGap, excluded, exclReason).
    rc2f: chainable == False rows are excluded (reason not_chainable); ratioSource assumed_1to1/stitched_1to1/rc2f_estimate* count as assumed."""
    if lineage_df is not None:
        l = lineage_df[lineage_df.relation.isin(LINK_TYPES)].copy()
        l["chainGap"] = pd.to_numeric(l["chainGapTradingDays"], errors="coerce").fillna(-1).astype(int) if "chainGapTradingDays" in l else -1
        l["excluded"] = l.chainGap > MAX_CHAIN_GAP
        l["exclReason"] = np.where(l.excluded, f"chain_gap>{MAX_CHAIN_GAP}", "")
        if not legacy and "chainable" in l.columns:
            nc = ~l.chainable.fillna(False).astype(bool) & ~l.excluded
            l.loc[nc, "excluded"] = True; l.loc[nc, "exclReason"] = "not_chainable"
        l["ratioAssumed"] = l.ratio.isna()
        if not legacy and "ratioSource" in l.columns:
            src = l.ratioSource.fillna("").astype(str)
            l["ratioAssumed"] = l.ratioAssumed | src.isin(["assumed_1to1", "stitched_1to1"]) | src.str.startswith("rc2f_estimate")
        l["ratio"] = l.ratio.fillna(1.0)
        l["conf"] = l.confidence.map({"high": 0, "medium": 1, "low": 2}).fillna(3)
        l = l.sort_values(["newId", "chainGap", "conf", "effectiveDate"], ascending=[True, True, True, False])
        l["linkVerified"] = l.linkVerified.map(lambda x: str(x).strip().lower() in ("true", "1", "yes")) if ("linkVerified" in l.columns and not legacy) else False
        keep = l[~l.excluded].drop_duplicates("newId")
        ex = l[l.excluded & ~l.newId.isin(keep.newId)].drop_duplicates("newId")
        return pd.concat([keep, ex], ignore_index=True)[["newId", "oldId", "ratio", "ratioAssumed", "chainGap", "excluded", "exclReason", "linkVerified"]]
    l = ldf[ldf.eventType.isin(LINK_TYPES) & ldf.childTradingItemId.notna()].copy()
    l["newId"] = l.childTradingItemId.astype("int64"); l["oldId"] = l.tradingItemId.astype("int64")
    l["ratio"] = pd.to_numeric(l.parentFactor).fillna(1.0); l["ratioAssumed"] = l.status.astype(str).eq("estimated") | l.isEstimate.fillna(False).astype(bool)
    l["chainGap"] = -1; l["excluded"] = False; l["exclReason"] = ""; l["linkVerified"] = False
    l["rank"] = l.status.map({"ok": 0, "estimated": 1, "unresolved": 2}).fillna(3)
    l = l.sort_values(["newId", "rank", "exDate"], ascending=[True, True, False]).drop_duplicates("newId")
    return l[["newId", "oldId", "ratio", "ratioAssumed", "chainGap", "excluded", "exclReason", "linkVerified"]].reset_index(drop=True)


def _merge_v2_into_links(lk: pd.DataFrame, v2l: pd.DataFrame) -> pd.DataFrame:
    """Fill ret_v2/flag_v2/prevDate_v2/match of the recomputed link rows from the v2 rows collected at (newId, linkDate); a linked plan row
    without a v2 row (or with a different prevDate) is a mismatch."""
    if len(lk) == 0:
        return lk
    lk = lk.copy()
    if len(v2l):
        v2l = v2l.copy(); v2l["date"] = pd.to_datetime(v2l.date)
        vm = v2l.set_index(["newId", "date"])
    else:
        vm = None
    for i in lk.index:
        if lk.at[i, "state"] != "linked":
            continue
        key = (int(lk.at[i, "newId"]), pd.Timestamp(lk.at[i, "linkDate"]))
        if vm is not None and key in vm.index:
            v = vm.loc[key]
            ret = float(lk.at[i, "ret_recomputed"]); flag = int(lk.at[i, "flag_expected"])
            lk.at[i, "ret_v2"] = float(v.ret_v2); lk.at[i, "flag_v2"] = int(v.flag_v2); lk.at[i, "prevDate_v2"] = pd.Timestamp(v.prevDate_v2)
            ok_ret = (np.isfinite(ret) and np.isfinite(float(v.ret_v2)) and abs(ret - float(v.ret_v2)) <= 1e-12 * max(1.0, abs(ret)))
            lk.at[i, "match"] = bool(ok_ret and int(v.flag_v2) == flag and pd.Timestamp(v.prevDate_v2) == pd.Timestamp(lk.at[i, "prevDate"]))
        else:
            lk.at[i, "match"] = False
    return lk


def link_recompute(base: Path, ldf: pd.DataFrame, lineage_df: pd.DataFrame | None, v2l: pd.DataFrame, memb: "Membership",
                   lifecycle_df: pd.DataFrame | None = None, legacy: bool = False) -> pd.DataFrame:
    """Independent per-link recomputation of the linked row (rework F3; rc2f: link row = first new-id row after the old id's
    lifecycle-capped last trade when the old id still trades on/after the new id's first print, else the first row); returns lineage_links.csv rows."""
    cand = _link_candidates(ldf, lineage_df, legacy=legacy)
    if len(cand) == 0:
        return pd.DataFrame()
    ids = np.unique(np.r_[cand.newId.to_numpy(np.int64), cand.oldId.to_numpy(np.int64)])
    bd = pq.read_table(base, columns=["tradingItemId", "date", "close"], filters=[("tradingItemId", "in", ids.tolist())]).to_pandas()
    bd = bd[np.isfinite(bd.close.to_numpy())].sort_values(["tradingItemId", "date"])
    bd["close"] = bd.close.astype(np.float64)
    if lifecycle_df is not None and not legacy and len(bd):
        ltd = pd.to_datetime(bd.tradingItemId.map(lifecycle_df.set_index("tradingItemId").lastTradeDate)).to_numpy().astype("datetime64[ns]")
        bd = bd[pd.isna(ltd) | (bd.date.to_numpy().astype("datetime64[ns]") <= ltd)]
    by_id = {tid: g[["date", "close"]].reset_index(drop=True) for tid, g in bd.groupby("tradingItemId")}
    ev = ldf[applied_mask(ldf, lifecycle=lifecycle_df, legacy=legacy)].copy()
    ev["exDate"] = pd.to_datetime(ev.exDate); ev["pf"] = pd.to_numeric(ev.parentFactor).fillna(1.0)
    ev["cps"] = ev.cashPerShare.fillna(0.0); ev["cval"] = ev.childValuePerOldShare.fillna(0.0)
    ev["flag"] = leg_flags(ev); ev["q"] = (ev.pf != 1.0).astype(int)
    ev["child"] = pd.to_numeric(ev.childTradingItemId, errors="coerce").fillna(-1).astype("int64")
    ev = ev.sort_values(["tradingItemId", "exDate", "q"], kind="stable")
    ev_by = {tid: g for tid, g in ev.groupby("tradingItemId")}
    v2map = v2l.set_index("newId") if len(v2l) else None

    def legs(tid, lo, hi):
        g = ev_by.get(tid)
        if g is None:
            return g if g is not None else ev.iloc[0:0]
        return g[(g.exDate > lo) & (g.exDate <= hi)]

    rows = []
    for c in cand.itertuples(index=False):
        new_id, old_id = int(c.newId), int(c.oldId)
        r = {"newId": new_id, "oldId": old_id, "ratio": float(c.ratio), "ratioAssumed": bool(c.ratioAssumed), "chainGap": int(c.chainGap),
             "state": "", "newFirstDate": pd.NaT, "linkDate": pd.NaT, "linkMode": "", "prevDate": pd.NaT, "prevClose": np.nan, "closeFirst": np.nan, "overlap": False,
             "gapN": 0, "gapLegs": "", "convPF": np.nan, "ratioConflict": False, "newLegsN": 0, "dedupN": 0,
             "ret_recomputed": np.nan, "flag_expected": -1, "ret_v2": np.nan, "flag_v2": -1, "prevDate_v2": pd.NaT, "match": True,
             "suspect": "", "oldSameDayRet": np.nan, "top500_first": False}
        if c.excluded:
            r["state"] = str(c.exclReason) if str(c.exclReason) else f"chain_gap>{MAX_CHAIN_GAP}"
        else:
            gn, go = by_id.get(new_id), by_id.get(old_id)
            if gn is None or go is None:
                r["state"] = "id_without_close"
            else:
                first = gn.date.iloc[0]; r["newFirstDate"] = first
                old_last = go.date.iloc[-1]
                link_date, mode = first, "first_row"
                if not legacy and old_last >= first:                       # when-issued overlap: link at the first new-id row after the old id's last trade
                    after = gn[gn.date > old_last]
                    if len(after) == 0:
                        r["state"] = "unresolvable_old_outlives_new"; r["linkMode"] = "old_outlives_new"; r["overlap"] = True
                        rows.append(r); continue
                    link_date, mode = after.date.iloc[0], "after_old_last"
                before = go[go.date < link_date]
                r["linkDate"] = link_date; r["linkMode"] = mode
                if len(before) == 0:
                    r["state"] = "unresolvable_no_old_close_before_first"
                else:
                    prev_date, prev_close = before.date.iloc[-1], float(before.close.iloc[-1])
                    link_close = float(gn[gn.date == link_date].close.iloc[0])
                    mirror = False
                    if mode == "after_old_last":
                        same = gn[gn.date == prev_date]
                        if len(same) and abs(prev_close / float(same.close.iloc[0]) - 1.0) <= prm.MIRROR_TAIL_TOL:
                            mirror = True; mode = "after_old_last_mirror"; r["linkMode"] = mode
                    r.update({"state": "linked", "prevDate": prev_date, "prevClose": prev_close, "closeFirst": link_close,
                              "overlap": bool(old_last >= first) and mode == "first_row"})
                    same_day = go[go.date == link_date]
                    if len(same_day):
                        r["oldSameDayRet"] = float(same_day.close.iloc[0]) / prev_close - 1.0
                    shares, cash, child, flag, has_q = 1.0, 0.0, 0.0, 0, False
                    ol = legs(old_id, prev_date, link_date)
                    keys = set()
                    for e in ol.itertuples(index=False):
                        cash += shares * e.cps; child += shares * e.cval; shares *= e.pf; has_q |= e.pf != 1.0; flag = max(flag, int(e.flag))
                        keys.add((e.exDate, e.legType, int(e.child), round(e.cps, 6), round(e.cval, 6), round(e.pf, 9)))
                    r["gapN"] = int(len(ol)); r["gapLegs"] = "|".join(f"{e.eventType}:{e.legType}:{e.cps:g}:{e.cval:g}:{e.pf:g}" for e in ol.itertuples(index=False))
                    # rc2f-E: a mirror tail keeps convPF 1 unless the lineage ratio is SOURCED, != 1 and the new id's own step across the link is
                    # better explained by the conversion than by continuity (|ratio x step - 1| <= |step - 1|: tail in old units, reverse split at the relist)
                    sourced_ne1 = (not bool(c.ratioAssumed)) and abs(float(c.ratio) - 1.0) > 1e-9
                    old_units = False
                    if mirror and sourced_ne1:
                        same = gn[gn.date == prev_date]
                        if len(same) and float(same.close.iloc[0]) > 0:
                            step = link_close / float(same.close.iloc[0])
                            old_units = abs(float(c.ratio) * step - 1.0) <= abs(step - 1.0)
                    conv = shares if has_q else (1.0 if (mirror and not (sourced_ne1 and old_units)) else float(c.ratio))
                    conflict = has_q and not bool(c.ratioAssumed) and abs(shares - float(c.ratio)) > RATIO_CONFLICT_TOL * max(1.0, float(c.ratio))
                    link_flag = 0 if (has_q and bool(c.ratioAssumed)) else (3 if bool(c.ratioAssumed) else 0)
                    flag = max(flag, link_flag)
                    if conflict:
                        flag = 4
                    shares = conv
                    nl = legs(new_id, prev_date, link_date)
                    ndup = 0
                    for e in nl.itertuples(index=False):
                        k = (e.exDate, e.legType, int(e.child), round(e.cps, 6), round(e.cval, 6), round(e.pf, 9))
                        if k in keys:
                            ndup += 1; continue
                        # rc2f-E: the new id's link-day quantity leg equal to the link conversion is the same reverse split -> applied once (WTW)
                        if e.legType == "quantity" and pd.Timestamp(e.exDate) == pd.Timestamp(link_date) and abs(conv - 1.0) > 1e-9 and abs(float(e.pf) - conv) <= RATIO_CONFLICT_TOL * max(1.0, conv):
                            ndup += 1; continue
                        cash += shares * e.cps; child += shares * e.cval; shares *= e.pf; flag = max(flag, int(e.flag))
                    ret = (shares * link_close + cash + child) / prev_close - 1.0
                    suspect = ""
                    if abs(ret) > LINK_RET_SUSPECT and bool(getattr(c, "linkVerified", False)):
                        suspect = "link_verified"                                       # rc2f-E: X5-verified genuine first-day move keeps the regular flag
                    elif abs(ret) > LINK_RET_SUSPECT:
                        flag = 4; suspect = "link_implausible_return"
                    elif r["overlap"]:
                        flag = max(flag, 3); suspect = "overlap"
                    r.update({"convPF": conv, "ratioConflict": bool(conflict), "newLegsN": int(len(nl)), "dedupN": ndup, "ret_recomputed": ret, "flag_expected": int(flag), "suspect": suspect,
                              "top500_first": bool(memb.member("Top500", np.array([new_id]), _days(np.array([link_date], dtype="datetime64[ns]")))[0])})
                    if conflict:
                        r["suspect"] = (r["suspect"] + ";" if r["suspect"] else "") + "ratio_conflict"
                    if (len(nl) - ndup) > 0 and conv != 1.0:
                        r["suspect"] = (r["suspect"] + ";" if r["suspect"] else "") + "newid_leg_with_conversion"
                        r["flag_expected"] = max(int(r["flag_expected"]), 3)
                    if v2map is not None and new_id in v2map.index:
                        v = v2map.loc[new_id]
                        r.update({"ret_v2": float(v.ret_v2), "flag_v2": int(v.flag_v2), "prevDate_v2": pd.Timestamp(v.prevDate_v2)})
                        ok_ret = (np.isfinite(ret) and np.isfinite(float(v.ret_v2)) and abs(ret - float(v.ret_v2)) <= 1e-12 * max(1.0, abs(ret)))
                        r["match"] = bool(ok_ret and int(v.flag_v2) == int(flag) and pd.Timestamp(v.prevDate_v2) == prev_date)
                    else:
                        r["match"] = False
        rows.append(r)
    return pd.DataFrame(rows)


def extra_lists(vd: pd.DataFrame, build: Path, memb: "Membership", out: Path) -> None:
    """rework F7: ledger gap candidates (vendor dividend step, no ledger event) with ciqdividend rows +-7d;
    rework F6: settlement-like cash rows (cash >= 0.9 x P_prev while the position keeps >= 50% of P_prev)."""
    gap = vd[(vd["class"] == "unresolved") & vd.reason.str.startswith("vendor dividend step without")].copy()
    if len(gap):
        gap["vendor_step_df_prev_over_df_t"] = gap.df_prev / gap.df_t
        dv_p = build / "dividends" / "dividends_long.parquet"
        gap["n_ciqdividend_pm7"] = 0; gap["ciqdividend_pm7"] = ""
        if dv_p.exists():
            dv = pd.read_parquet(dv_p, columns=["tradingItemId", "exDate", "divAmount", "dividendTypeId", "isCash"])
            dv = dv[dv.tradingItemId.isin(gap.tradingItemId.unique())]
            for i, r in gap.iterrows():
                x = dv[(dv.tradingItemId == r.tradingItemId) & (dv.exDate >= r.date - pd.Timedelta(days=7)) & (dv.exDate <= r.date + pd.Timedelta(days=7))]
                gap.at[i, "n_ciqdividend_pm7"] = int(len(x))
                gap.at[i, "ciqdividend_pm7"] = ";".join(f"{d.date()}:{a}:type{t}:cash={c}" for d, a, t, c in zip(x.exDate, x.divAmount, x.dividendTypeId, x.isCash))
        cols = ["tradingItemId", "date", "prevDate", "vendor_step_df_prev_over_df_t", "vendor_implied_value", "close_prev", "ret_v2", "ret_rc1", "retFlag",
                "types", "n_ciqdividend_pm7", "ciqdividend_pm7", "Top500", "Top1000", "reason"]
        gap[cols].sort_values(["tradingItemId", "date"]).to_csv(out / "ledger_gap_candidates.csv", index=False)
    sett = vd[(vd.n_cash > 0) & (vd.cash_sum >= 0.9 * vd.close_prev) & (vd.pf_prod * vd.close_t >= 0.5 * vd.close_prev)].copy()
    if len(sett):
        cols = ["tradingItemId", "date", "prevDate", "close_prev", "close_t", "cash_sum", "pf_prod", "ret_v2", "ret_rc1", "retFlag", "types", "statuses", "sources", "is_last_obs", "Top500", "Top1000", "class", "reason"]
        sett[cols].sort_values(["is_last_obs", "tradingItemId"], ascending=[False, True]).to_csv(out / "settlement_like_cash_rows.csv", index=False)


def classify_vendor_diff(vd: pd.DataFrame) -> pd.DataFrame:
    """Class + reason per diff cell. Precedence: unresolved > estimate > split-ratio mismatch > distribution > cash > no-ledger-event."""
    vd = vd.copy()
    step_df = vd.df_prev.to_numpy() / vd.df_t.to_numpy()            # vendor: 1 - D/P_prev on a dividend day
    step_sf = vd.sf_prev.to_numpy() / vd.sf_t.to_numpy()            # vendor split ratio over the interval
    vendor_has_div = np.abs(step_df - 1.0) > 1e-12
    vendor_has_split = np.abs(step_sf - 1.0) > 1e-12
    implied_D = vd.close_prev.to_numpy() * (1.0 - step_df)
    vd["vendor_implied_value"] = implied_D
    vd["vendor_has_div_step"] = vendor_has_div
    vd["vendor_has_split_step"] = vendor_has_split
    n_legs = vd.n_legs.to_numpy(); n_value = vd.n_value.to_numpy(); n_cash = vd.n_cash.to_numpy()
    n_cash_ciq = vd.n_cash_ciqdividend.to_numpy() if "n_cash_ciqdividend" in vd else n_cash
    n_cash_man = vd.n_cash_manual.to_numpy() if "n_cash_manual" in vd else np.zeros(len(vd), int)
    srcs = vd.sources.to_numpy() if "sources" in vd else np.full(len(vd), "", dtype=object)
    mf = vd.max_flag.to_numpy(); rep = vd.any_repair.to_numpy()
    cash = vd.cash_sum.to_numpy(); child = vd.child_sum.to_numpy(); pfp = vd.pf_prod.to_numpy()
    pf_ok = np.abs(pfp - step_sf) <= 1e-9 * np.maximum(1.0, step_sf)
    cash_ok = vendor_has_div & (np.abs(implied_D - cash) <= CASH_REL_TOL * np.abs(cash) + 1e-4)
    legs = vd.legs.to_numpy()
    povr = vd.price_override.to_numpy(dtype=bool) if "price_override" in vd else np.zeros(len(vd), bool)
    lnk = vd.linked.to_numpy(dtype=bool) if "linked" in vd else np.zeros(len(vd), bool)
    sovr = vd.split_override.fillna("").astype(str).to_numpy() if "split_override" in vd else np.full(len(vd), "", dtype=object)
    sost = vd.split_override_status.fillna("").astype(str).to_numpy() if "split_override_status" in vd else np.full(len(vd), "", dtype=object)
    rv2 = vd.ret_v2.to_numpy(); rr1 = vd.ret_rc1.to_numpy()
    cls = []; reason = []
    for i in range(len(vd)):
        if sovr[i]:
            # R-X0 2nd review P1-7: a corrected split boundary in (prevDate, date] — the vendor (rc2 original factors) step differs by construction;
            # listed with the override id so a wrong drop/move_date (large |ret_v2| with no price move) is visible, never hidden as 'unresolved'
            unv = sost[i] == "applied_unverified"; unc = sost[i] == "applied_unchecked"
            cls.append("repaired_factor(csv,unverified)" if unv else ("repaired_factor(csv,unchecked)" if unc else "repaired_factor(csv)"))
            reason.append(f"split override {sovr[i]} ({sost[i]}) at a boundary in (prevDate, date]: vendor split ratio {step_sf[i]:g} vs ledger {pfp[i]:g}; "
                          f"ret_v2 {rv2[i]:+.4g} vs rc1 {rr1[i]:+.4g}" + (" — raw price check FAILED in apply (see rc2f_apply_log reason)" if unv else
                                                                          (" — raw price check not possible in apply (no raw close within ±5 trading days)" if unc else "")))
        elif povr[i]:
            cls.append("price_override"); reason.append(f"base_data close corrected by rc2f price override (qualityFlag {QF_PRICE_OVERRIDE}) on date or prevDate; vendor rc1 used the old close")
        elif lnk[i] and mf[i] < 3:
            cls.append("link_row"); reason.append("lineage link row: prev = old id last close + old-id gap legs (rc2f after_old_last link), rc1 = new id plain price return")
        elif mf[i] == 4:
            cls.append("unresolved"); reason.append("ledger leg unresolved (value incomplete): " + legs[i])
        elif mf[i] == 3:
            cls.append("estimate"); reason.append("ledger leg estimated: " + legs[i])
        elif n_legs[i] > 0 and not pf_ok[i]:
            cls.append("repaired_factor" if rep[i] else "unresolved")
            reason.append(("split ratio repaired vs vendor (repairOf set): " if rep[i] else "split product != vendor split ratio, no repairOf: ")
                          + f"ledger {pfp[i]:g} vs vendor {step_sf[i]:g}")
        elif n_value[i] > 0:
            if vendor_has_div[i]:
                cls.append("distribution_formula")
                reason.append(f"cash-style distribution (child close x ratio + cash) vs vendor reinvestment step; vendor implied {implied_D[i]:.6g} vs ledger {cash[i] + child[i]:.6g}")
            else:
                cls.append("repaired_factor")
                reason.append(f"distribution missing in vendor factor (no step); ledger value {cash[i] + child[i]:.6g}")
        elif n_cash[i] > 0:
            # rework F6: the ciqdividend label only for cash legs sourced from ciqdividend (cash_dividend/special_dividend);
            # manual_override cash legs are deliberate repairs; any other cash source is NOT explained here
            if n_cash_ciq[i] < n_cash[i] and n_cash_ciq[i] + n_cash_man[i] < n_cash[i]:
                cls.append("unexplained"); reason.append(f"cash leg from a non-ciqdividend source ({srcs[i]}): {legs[i]}")
            elif n_cash_ciq[i] < n_cash[i]:
                cls.append("repaired_factor"); reason.append(f"manual_override cash leg ({srcs[i]}); vendor implied {implied_D[i]:.6g} vs ledger {cash[i]:.6g}")
            elif cash_ok[i]:
                cls.append("cash_formula")
                reason.append(f"second-order (D/P) x r: cash-style vs vendor reinvestment, same D; vendor implied {implied_D[i]:.6g} vs ledger {cash[i]:.6g}")
            elif vendor_has_div[i]:
                cls.append("repaired_factor")
                reason.append(f"vendor step inconsistent with ciqdividend cash (P12: actual dividend event wins); vendor implied {implied_D[i]:.6g} vs ledger {cash[i]:.6g}")
            else:
                cls.append("repaired_factor")
                reason.append(f"vendor has no dividend step for a ciqdividend cash event; ledger {cash[i]:.6g}")
        elif n_legs[i] == 0:
            cls.append("repaired_factor" if rep[i] else "unresolved")
            if vendor_has_div[i]:
                reason.append(f"vendor dividend step without any ledger event; vendor implied {implied_D[i]:.6g}")
            elif vendor_has_split[i]:
                reason.append(f"vendor split step without ledger split row; ratio {step_sf[i]:g}")
            else:
                reason.append("no ledger event and no vendor step")
        elif vendor_has_div[i]:                                   # quantity legs only (split/stock dividend) + vendor dividend step
            cls.append("repaired_factor" if rep[i] else "unresolved")
            reason.append(f"vendor dividend step without ledger cash/value leg (ledger has only quantity legs {legs[i]}); vendor implied {implied_D[i]:.6g}")
        else:
            cls.append("unexplained"); reason.append("no rule matched: legs=" + legs[i])
    if "n_stitched" in vd:
        nst = vd.n_stitched.to_numpy()
        reason = [r + " [stitched copy of the trading id's leg]" if nst[i] > 0 else r for i, r in enumerate(reason)]
    vd["class"] = cls; vd["reason"] = reason
    return vd


def vendor_diff_tables(vd: pd.DataFrame) -> dict:
    out = {}
    by = vd.groupby("class").agg(cells=("diff", "size"), top500=("Top500", "sum"), top1000=("Top1000", "sum"), abs_diff_median=("diff", lambda s: float(np.median(np.abs(s)))),
                                 abs_diff_max=("diff", lambda s: float(np.abs(s).max())))
    out["by_class"] = {k: {kk: (int(vv) if kk in ("cells", "top500", "top1000") else float(vv)) for kk, vv in v.items()} for k, v in by.to_dict("index").items()}
    vd = vd.copy()
    vd["etype"] = vd.types.where(vd.types != "", "(none)")
    by2 = vd.groupby(["class", "etype"]).agg(cells=("diff", "size"), top500=("Top500", "sum")).reset_index().sort_values("cells", ascending=False).head(40)
    out["by_class_type"] = by2.to_dict("records")
    return out


def oracle_gate(build: Path, base: Path, v2: Path, ldf: pd.DataFrame, out: Path, rc1: Path | None = None) -> dict:
    ids = sorted({c[1] for c in ORACLE} | {cid for c in ORACLE if c[3] in ("child",) for cid, _ in c[4]["children"]} | {PBCT_OLD})
    bd = pq.read_table(base, columns=["tradingItemId", "date", "close"], filters=[("tradingItemId", "in", ids)]).to_pandas()
    bd = bd[np.isfinite(bd.close.to_numpy())].sort_values(["tradingItemId", "date"])
    bd["close"] = bd.close.astype(np.float64)
    rv = pq.read_table(v2, filters=[("tradingItemId", "in", ids)]).to_pandas().sort_values(["tradingItemId", "date"])

    def close_at(tid, d):
        x = bd[(bd.tradingItemId == tid) & (bd.date == pd.Timestamp(d))]
        return float(x.close.iloc[0]) if len(x) else np.nan

    def close_before(tid, d):
        x = bd[(bd.tradingItemId == tid) & (bd.date < pd.Timestamp(d))]
        return float(x.close.iloc[-1]) if len(x) else np.nan

    def v2_at(tid, d):
        x = rv[(rv.tradingItemId == tid) & (rv.date == pd.Timestamp(d))]
        return (float(x.ret.iloc[0]), int(x.retFlag.iloc[0])) if len(x) else (np.nan, -1)

    rows = []
    for label, tid, d, kind, spec in ORACLE:
        got, fl = v2_at(tid, d)
        c_t, c_p = close_at(tid, d), close_before(tid, d)
        exp = np.nan; tol = 1e-4 if kind == "raw" else 1e-9; note = ""
        if kind == "raw":
            exp = spec["value"]; note = f"section 2 raw numbers ({spec['display']})"
        elif kind == "cash":
            exp = (c_t + spec["cash"]) / c_p - 1.0; note = f"(close {c_t:g} + cash {spec['cash']}) / prev {c_p:g} - 1"
        elif kind in ("child", "unres"):
            children = spec.get("children", [])
            if kind == "unres":
                lrow = ldf[(ldf.tradingItemId == tid) & (pd.to_datetime(ldf.exDate) == pd.Timestamp(d)) & (ldf.legType == "stock")]
                if len(lrow) and lrow.status.iloc[0] != "unresolved" and pd.notna(lrow.childTradingItemId.iloc[0]):
                    children = [(int(lrow.childTradingItemId.iloc[0]), float(lrow.childRatio.iloc[0]))]
                    bd2 = pq.read_table(base, columns=["tradingItemId", "date", "close"], filters=[("tradingItemId", "==", children[0][0])]).to_pandas()
                    bd = pd.concat([bd, bd2[np.isfinite(bd2.close.to_numpy())]], ignore_index=True)
                else:
                    ok = fl == 4
                    rows.append({"case": label, "tradingItemId": tid, "date": d, "expected": "retFlag 4 (child unpriced in this ledger)", "got": got, "retFlag": fl, "ok": ok,
                                 "note": "ledger status " + (lrow.status.iloc[0] if len(lrow) else "no row")})
                    continue
            cv = sum(r * close_at(cid, d) for cid, r in children)
            exp = (spec["pf"] * c_t + cv) / c_p - 1.0
            note = f"({spec['pf']:g} x {c_t:g} + " + " + ".join(f"{r:g} x {close_at(cid, d):g}" for cid, r in children) + f") / {c_p:g} - 1"
        # value must match; the row must be event-valued (flag 1/2) or event-valued with an estimated leg marked by the
        # ledger (flag 3, e.g. a split row whose status is R1's confidence marker) -- never price-only (0) or unresolved (4)
        ok = np.isfinite(got) and np.isfinite(exp) and abs(got - exp) <= tol and fl in (1, 2, 3)
        rows.append({"case": label, "tradingItemId": tid, "date": d, "expected": exp, "got": got, "abs_err": abs(got - exp) if np.isfinite(got) and np.isfinite(exp) else np.nan,
                     "tol": tol, "retFlag": fl, "ok": bool(ok), "note": note})
    # PBCT old line 2000-2007: every cash-event day equals the cash formula with the ledger (ciqdividend) amount; no vendor scale
    p = rv[(rv.tradingItemId == PBCT_OLD) & (rv.date >= "2000-01-01") & (rv.date <= "2007-12-31")].copy()
    pb = bd[bd.tradingItemId == PBCT_OLD].set_index("date").close
    lc = ldf[(ldf.tradingItemId == PBCT_OLD) & (ldf.legType == "cash")].copy(); lc["exDate"] = pd.to_datetime(lc.exDate)
    lc = lc[(lc.exDate >= "2000-01-01") & (lc.exDate <= "2007-12-31")]
    n_ok = 0; n_bad = 0; max_dp = 0.0
    for _, r in lc.iterrows():
        x = p[p.date >= r.exDate].head(1)
        if not len(x):
            continue
        d = x.date.iloc[0]; prev = x.prevDate.iloc[0]
        if pd.isna(prev):
            continue
        exp = (pb.loc[d] + r.cashPerShare) / pb.loc[prev] - 1.0
        max_dp = max(max_dp, r.cashPerShare / pb.loc[prev])
        if abs(float(x.ret.iloc[0]) - exp) <= 1e-12:
            n_ok += 1
        else:
            n_bad += 1
    rc1_p = pq.read_table(Path(rc1) if rc1 is not None else build / "derived" / "returns_total.parquet", filters=[("tradingItemId", "==", PBCT_OLD)]).to_pandas()   # rc1 벤더식(--rc1; rc2f 는 build 가 새 빌드)
    rc1_p = rc1_p[(rc1_p.date >= "2000-01-01") & (rc1_p.date <= "2007-12-31")]
    cum_v2 = float(np.prod(1 + p.ret.fillna(0))); cum_rc1 = float(np.prod(1 + rc1_p.ret.fillna(0)))
    # no-cash cumulative = price ratio x split product over the same rows (ledger split rows)
    ls = ldf[(ldf.tradingItemId == PBCT_OLD) & (ldf.eventType == "split")].copy(); ls["exDate"] = pd.to_datetime(ls.exDate)
    ls = ls[(ls.exDate > p.date.min()) & (ls.exDate <= p.date.max())]
    cum_nocash = float(pb.loc[p.date.max()] / pb.loc[p.date.min()] * np.prod(ls.parentFactor)) if len(p) else np.nan
    sum_dp = float(sum(r.cashPerShare / pb.loc[pb.index[pb.index < r.exDate][-1]] for _, r in lc.iterrows() if (pb.index < r.exDate).any()))
    yield_v2 = cum_v2 / cum_nocash; yield_rc1 = cum_rc1 / cum_nocash
    ok_p = n_bad == 0 and n_ok == len(lc) and 1.0 < yield_v2 < 1.0 + 1.5 * sum_dp and yield_rc1 / yield_v2 > 2.0
    rows.append({"case": "PBCT 215291786 2000-2007: cash days = (close + ciqdividend D)/prev - 1; vendor x2.98 scale removed", "tradingItemId": PBCT_OLD, "date": "2000..2007",
                 "expected": f"all {len(lc)} cash days match 1e-12; cum TR / cum(price x split) in (1, 1 + 1.5 x sum D/P = {1 + 1.5 * sum_dp:.3f}); rc1 ratio > 2x ours",
                 "got": f"match {n_ok}, mismatch {n_bad}; cum v2 {cum_v2:.4f}, rc1 {cum_rc1:.4f}, price x split {cum_nocash:.4f}; dividend accumulation v2 {yield_v2:.4f} vs rc1 {yield_rc1:.4f} (sum D/P {sum_dp:.4f}); max D/P {max_dp:.4f}",
                 "retFlag": None, "ok": bool(ok_p), "note": "rc1 T7: cumulative total-return bias x2.977"})
    tab = pd.DataFrame(rows)
    tab.to_csv(out / "oracle.csv", index=False)
    return {"pass": bool(tab.ok.all()), "n_cases": int(len(tab)), "n_ok": int(tab.ok.sum()), "cases": tab.to_dict("records")}


# --------------------------------------------------------------------------------------
# self-test: the classifier and gate logic must fail on planted errors
# --------------------------------------------------------------------------------------
def self_test() -> list[dict]:
    res = []

    def rec(name, ok, got):
        res.append({"case": name, "ok": bool(ok), "got": got})

    base = {"tradingItemId": 1, "date": pd.Timestamp("2020-01-10"), "prevDate": pd.Timestamp("2020-01-09"), "ret_v2": 0.0, "ret_rc1": 0.0, "diff": 0.0,
            "retFlag": 1, "close_t": 100.0, "close_prev": 100.0, "sf_t": 1.0, "sf_prev": 1.0, "df_t": 1.0, "df_prev": 0.99, "linked": False,
            "n_legs": 1, "n_cash": 1, "n_value": 0, "n_split": 0, "cash_sum": 1.0, "child_sum": 0.0, "pf_prod": 1.0, "max_flag": 1, "any_repair": False,
            "types": "cash_dividend", "statuses": "ok", "legs": "cash_dividend:cash:1.0:nan:1:ok", "Top500": False, "Top1000": False,
            "n_cash_ciqdividend": 1, "n_cash_manual": 0, "sources": "ciqdividend"}
    cases = [
        ("cash_formula", {}),
        ("repaired_factor", {"df_prev": 0.97}),                                              # vendor implied 3.0 vs ledger 1.0
        ("repaired_factor", {"df_prev": 1.0}),                                               # no vendor step for a ledger dividend
        ("distribution_formula", {"n_value": 1, "n_cash": 0, "child_sum": 10.0, "cash_sum": 0.0, "max_flag": 2, "df_prev": 0.9}),
        ("repaired_factor", {"n_value": 1, "n_cash": 0, "child_sum": 10.0, "cash_sum": 0.0, "max_flag": 2, "df_prev": 1.0}),   # vendor missed the distribution
        ("estimate", {"max_flag": 3}),
        ("unresolved", {"max_flag": 4}),
        ("unresolved", {"n_legs": 0, "n_cash": 0, "cash_sum": 0.0, "max_flag": 0, "types": "", "legs": ""}),                 # vendor step, no ledger event
        ("unresolved", {"n_legs": 1, "n_cash": 0, "n_split": 1, "pf_prod": 2.0, "sf_prev": 4.0, "cash_sum": 0.0, "max_flag": 0, "types": "split"}),  # split product != vendor
        ("repaired_factor", {"n_legs": 1, "n_cash": 0, "n_split": 1, "pf_prod": 2.0, "sf_prev": 4.0, "cash_sum": 0.0, "max_flag": 0, "types": "split", "any_repair": True}),
        ("unexplained", {"n_cash_ciqdividend": 0, "sources": "corporate_actions", "types": "other", "df_prev": 1.0}),      # F6: PVT-like cash not from ciqdividend (R-X0 P0-1: no settlement-cash class)
        ("repaired_factor", {"n_cash_ciqdividend": 0, "n_cash_manual": 1, "sources": "manual_override", "df_prev": 1.0}),   # F6: manual repair
        ("price_override", {"price_override": True}),                                                                     # rc2f: corrected close on date/prevDate
        ("link_row", {"linked": True, "n_legs": 0, "n_cash": 0, "cash_sum": 0.0, "max_flag": 0, "types": "", "legs": "", "df_prev": 1.0}),   # rc2f: after_old_last link row
        # R-X0 2nd review P1-7: a dropped vendor boundary (vendor step 2, no ledger split row, +100% price-only return) is NOT 'unresolved' but the override
        ("repaired_factor(csv)", {"n_legs": 0, "n_cash": 0, "cash_sum": 0.0, "max_flag": 0, "types": "", "legs": "", "df_prev": 1.0, "sf_prev": 2.0, "ret_v2": 1.0,
                                  "split_override": "X1-7", "split_override_status": "applied"}),
        ("repaired_factor(csv,unverified)", {"n_legs": 0, "n_cash": 0, "cash_sum": 0.0, "max_flag": 0, "types": "", "legs": "", "df_prev": 1.0, "sf_prev": 2.0, "ret_v2": 1.0,
                                             "split_override": "X1-8", "split_override_status": "applied_unverified"}),
        ("repaired_factor(csv,unchecked)", {"n_legs": 0, "n_cash": 0, "cash_sum": 0.0, "max_flag": 0, "types": "", "legs": "", "df_prev": 1.0, "sf_prev": 2.0, "ret_v2": 1.0,
                                            "split_override": "X1-9", "split_override_status": "applied_unchecked"}),
    ]
    vd = pd.DataFrame([{**base, "price_override": False, "split_override": "", "split_override_status": "", **o} for _, o in cases])
    got = classify_vendor_diff(vd)["class"].tolist()
    exp = [c for c, _ in cases]
    rec("classifier: 17 planted cells get the expected classes", got == exp, list(zip(exp, got)))
    rec("classifier: unexplained appears only for the planted non-ciqdividend cash leg", got.count("unexplained") == 1, got)
    # Membership lookup
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "m.parquet"
        pd.DataFrame({"universe": ["Top500", "Top500", "Top1000"], "tradingItemId": [1, 1, 2], "fromDate": pd.to_datetime(["2020-01-01", "2021-01-01", "2020-01-01"]),
                      "toDate": pd.to_datetime(["2020-06-30", "2021-12-31", "2020-12-31"])}).to_parquet(p, index=False)
        mb = Membership(p)
        got = mb.member("Top500", np.array([1, 1, 1, 2]), _days(np.array(["2020-03-01", "2020-09-01", "2021-05-01", "2020-03-01"], dtype="datetime64[ns]")))
        rec("membership: inside/gap/inside/other-universe", got.tolist() == [True, False, True, False], got.tolist())
    # interval summary: exclusive prev, inclusive date, product/sum semantics
    ldf = pd.DataFrame([
        {"tradingItemId": 1, "exDate": pd.Timestamp("2020-01-08"), "eventType": "split", "legType": "quantity", "cashPerShare": np.nan, "parentFactor": 2.0, "childTradingItemId": pd.NA, "childValuePerOldShare": np.nan, "status": "ok", "isEstimate": False, "childValueSource": "unknown", "repairOf": None},
        {"tradingItemId": 1, "exDate": pd.Timestamp("2020-01-09"), "eventType": "cash_dividend", "legType": "cash", "cashPerShare": 1.0, "parentFactor": 1.0, "childTradingItemId": pd.NA, "childValuePerOldShare": np.nan, "status": "ok", "isEstimate": False, "childValueSource": "unknown", "repairOf": None},
        {"tradingItemId": 1, "exDate": pd.Timestamp("2020-01-06"), "eventType": "cash_dividend", "legType": "cash", "cashPerShare": 5.0, "parentFactor": 1.0, "childTradingItemId": pd.NA, "childValuePerOldShare": np.nan, "status": "ok", "isEstimate": False, "childValueSource": "unknown", "repairOf": None},
    ])
    s = ledger_interval_summary(ldf, np.array([1, 1]), _days(np.array(["2020-01-06", "2020-01-08"], dtype="datetime64[ns]")), _days(np.array(["2020-01-09", "2020-01-08"], dtype="datetime64[ns]")))
    rec("interval summary: (01-06, 01-09] -> split+cash (pf 2, cash 1); (01-08, 01-08] -> nothing", s.n_legs.tolist() == [2, 0] and s.pf_prod.iloc[0] == 2.0 and s.cash_sum.iloc[0] == 1.0, s[["n_legs", "pf_prod", "cash_sum"]].to_dict("records"))
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build", type=Path, help="rc1 build dir (base_data, factors/, derived/returns_total rc1, universe/)")
    ap.add_argument("--v2", type=Path)
    ap.add_argument("--ledger", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--rc1", type=Path, default=None)
    ap.add_argument("--base-data", type=Path, default=None)
    ap.add_argument("--lineage", type=Path, default=None, help="lineage file used by the generator (R3 lineage v2); default: ledger rename/relist rows")
    ap.add_argument("--stitched-copies", default=None, help="as in prices_returns: R1 ca_ledger_stitched_copies.csv (default auto next to the ledger; 'off')")
    ap.add_argument("--lifecycle", type=Path, default=None, help="master/lifecycle.parquet used by the generator for the link rule (default <build>/master/lifecycle.parquet)")
    ap.add_argument("--legacy-links", action="store_true", help="rc2 link rule (as prices_returns --legacy-links)")
    ap.add_argument("--vendor-factors", type=Path, default=None, help="VENDOR split factors for G-SPLIT/G-VENDOR (rc2 original factors/split.parquet, before rc2f split overrides); default <build>/factors/split.parquet")
    ap.add_argument("--rc2f-stage", type=Path, default=None, help="rc2f_apply output dir: applied split overrides classify cells at corrected boundaries as repaired_factor(csv[,unverified]) and list them (split_override_cells.csv)")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--threads", type=int, default=8)
    args = ap.parse_args(argv)
    pa.set_cpu_count(args.threads)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.self_test:
        res = self_test()
        for r in res:
            print(("OK  " if r["ok"] else "BAD ") + r["case"], "| got", r["got"])
        n_bad = sum(not r["ok"] for r in res)
        print(f"self-test: {len(res) - n_bad}/{len(res)} cases behave as expected")
        return 1 if n_bad else 0
    if not (args.build and args.v2 and args.ledger and args.out):
        ap.error("--build, --v2, --ledger, --out are required")
    sc = False if args.stitched_copies == "off" else (Path(args.stitched_copies) if args.stitched_copies else None)
    G = run_gates(args.build, args.v2, args.ledger, args.out, args.rc1, args.base_data, lineage=args.lineage, stitched_copies=sc, lifecycle=args.lifecycle, legacy_links=args.legacy_links,
                  vendor_factors=args.vendor_factors, rc2f_stage=args.rc2f_stage)
    summary = {k: (v if k != "G-ORACLE" else {"pass": v["pass"], "n_ok": v["n_ok"], "n_cases": v["n_cases"]}) for k, v in G.items() if k != "G-VENDOR"}
    summary["G-LINK"] = {k: v for k, v in G["G-LINK"].items() if k not in ("only_v2", "only_recomputed")}
    summary["G-VENDOR"] = {k: v for k, v in G["G-VENDOR"].items() if k != "by_type"}
    print(json.dumps(summary, indent=1, default=str))
    for c in G["G-ORACLE"]["cases"]:
        print(("OK  " if c["ok"] else "BAD "), c["case"], "| expected", c["expected"], "| got", c["got"], "| flag", c["retFlag"])
    print("GATES PASS" if G["pass"] else "GATES FAIL")
    return 0 if G["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
