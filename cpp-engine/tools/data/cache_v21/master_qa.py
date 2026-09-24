#!/usr/bin/env python3
"""cache_v21/master_qa.py - T1 QA: id-mapping coverage report, gates and self-tests.

qa/id_mapping_coverage.md  four classes, each for ALL / everTop500 / everTop1000 (items and their trading days):
  A. no ciqgvkeyiid row at all
  B. mapping exists but leaves a gap inside the priced period [firstTradeDate, lastTradeDate]
  C. same-day multiple (gvkey, iid) candidates (conflictFlag)            -> Top500 items listed one by one
  D. mapping exists but no TIC/EXCHG row in sec_idhist for any of its (gvkey, iid)
Trading days are counted on the ciq trading calendar (union of close dates of long-lived items).

Gates (qa/gates.json, qa/gates.md):
  G1 two trading items of the same securityId both tradable on the same day (firstTradeDate <= d <= lastTradeDate)
     -> target 0, otherwise the (pair, days) list
  G2 no overlapping intervals inside a tradingItemId in identifier_history (per item), classification_history,
     primary_history, and id_mapping rows with conflictFlag == False
Each checker is first shown to fail on a planted counterexample.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import master_build as mb  # noqa: E402
import master_lifecycle as ml  # noqa: E402

log = logging.getLogger("cache_v21.qa")
NEG_INF = pd.Timestamp("1800-01-01")
POS_INF = pd.Timestamp("2900-01-01")


# --------------------------------------------------------------------------------------
# generic interval-overlap checker (+ planted counterexample)
# --------------------------------------------------------------------------------------
def overlaps_within(df: pd.DataFrame, key: str, start: str, end: str, group_extra: list[str] | None = None) -> pd.DataFrame:
    """Rows (pairs) of the same key (and group_extra) whose [start, end] intervals intersect. NaT end = open."""
    d = df[[key, start, end] + (group_extra or [])].copy()
    d["_s"] = pd.to_datetime(d[start]).astype("datetime64[ms]").fillna(NEG_INF)
    d["_e"] = pd.to_datetime(d[end]).astype("datetime64[ms]").fillna(POS_INF)
    d["_row"] = np.arange(len(d))
    d = d.sort_values([key] + (group_extra or []) + ["_s", "_e"])
    grp = [key] + (group_extra or [])
    prev_e = d.groupby(grp)["_e"].shift()
    prev_row = d.groupby(grp)["_row"].shift()
    hit = prev_e.notna() & (d["_s"] <= prev_e)
    out = d.loc[hit, grp + ["_s", "_e", "_row"]].copy()
    out["prevRow"] = prev_row[hit].astype("int64")
    out["prevEnd"] = prev_e[hit]
    return out.reset_index(drop=True)


def self_test_overlap() -> dict:
    ok = pd.DataFrame({"k": [1, 1, 2], "s": pd.to_datetime(["2020-01-01", "2020-06-02", "2019-01-01"]),
                       "e": pd.to_datetime(["2020-06-01", None, "2019-12-31"])})
    bad = pd.DataFrame({"k": [1, 1], "s": pd.to_datetime(["2020-01-01", "2020-05-01"]), "e": pd.to_datetime(["2020-06-01", None])})
    n_ok = len(overlaps_within(ok, "k", "s", "e"))
    n_bad = len(overlaps_within(bad, "k", "s", "e"))
    return {"clean_frame_overlaps": n_ok, "planted_frame_overlaps": n_bad, "pass": n_ok == 0 and n_bad == 1}


# --------------------------------------------------------------------------------------
# G1: same securityId, both tradable the same day
# --------------------------------------------------------------------------------------
def same_security_overlap(inst: pd.DataFrame, lc: pd.DataFrame, cal: np.ndarray) -> pd.DataFrame:
    d = inst[["tradingItemId", "securityId"]].merge(lc[["tradingItemId", "firstTradeDate", "lastTradeDate"]], on="tradingItemId")
    d = d[d.firstTradeDate.notna() & d.lastTradeDate.notna()]
    rows = []
    for sid, g in d.groupby("securityId"):
        if len(g) < 2:
            continue
        r = g.sort_values("firstTradeDate").to_dict(orient="records")
        for i in range(len(r)):
            for j in range(i + 1, len(r)):
                lo = max(r[i]["firstTradeDate"], r[j]["firstTradeDate"])
                hi = min(r[i]["lastTradeDate"], r[j]["lastTradeDate"])
                if lo <= hi:
                    n = int(ml.tdays_inclusive(cal, np.array([lo]), np.array([hi]))[0])
                    rows.append({"securityId": int(sid), "idA": int(r[i]["tradingItemId"]), "idB": int(r[j]["tradingItemId"]),
                                 "overlapFrom": lo, "overlapTo": hi, "overlapTradingDays": n})
    return pd.DataFrame(rows, columns=["securityId", "idA", "idB", "overlapFrom", "overlapTo", "overlapTradingDays"])


def self_test_same_security(cal: np.ndarray) -> dict:
    inst = pd.DataFrame({"tradingItemId": [1, 2, 3, 4], "securityId": [10, 10, 20, 20]})
    lc = pd.DataFrame({"tradingItemId": [1, 2, 3, 4],
                       "firstTradeDate": pd.to_datetime(["2010-01-04", "2015-01-05", "2010-01-04", "2011-06-01"]),
                       "lastTradeDate": pd.to_datetime(["2015-01-02", "2020-01-02", "2011-12-30", "2020-01-02"])})   # 3/4 overlap Jun-Dec 2011
    r = same_security_overlap(inst, lc, cal)
    return {"clean_pairs": int((r.securityId == 10).sum()), "planted_pairs": int((r.securityId == 20).sum()),
            "pass": bool((r.securityId == 10).sum() == 0 and (r.securityId == 20).sum() == 1)}


# --------------------------------------------------------------------------------------
# id-mapping coverage
# --------------------------------------------------------------------------------------
def universe_members(name: str) -> set[int]:
    p = mb.PROTO_UNIVERSE_DIR / f"{name}.parquet"
    t = pq.read_table(p)
    cols = [c for c in t.column_names if not c.startswith("__index")]
    keep = []
    for c in cols:
        arr = t.column(c).to_numpy(zero_copy_only=False)
        if np.nansum(arr.astype(float)) > 0:          # ever True
            keep.append(int(c))
    return set(keep)


def gap_days_in_period(cal: np.ndarray, first, last, intervals: list[tuple]) -> int:
    """Trading days in [first, last] not covered by any interval (NaT bounds unbounded)."""
    if pd.isna(first) or pd.isna(last) or last < first:
        return 0
    cal_ms = cal.astype("datetime64[ms]")
    i0 = int(np.searchsorted(cal_ms, np.datetime64(pd.Timestamp(first), "ms"), side="left"))
    i1 = int(np.searchsorted(cal_ms, np.datetime64(pd.Timestamp(last), "ms"), side="right"))
    if i1 <= i0:
        return 0
    covered = np.zeros(i1 - i0, dtype=bool)
    for s, e in intervals:
        s = NEG_INF if pd.isna(s) else pd.Timestamp(s)
        e = POS_INF if pd.isna(e) else pd.Timestamp(e)
        a = int(np.searchsorted(cal_ms, np.datetime64(s, "ms"), side="left")) - i0
        b = int(np.searchsorted(cal_ms, np.datetime64(e, "ms"), side="right")) - i0
        a, b = max(a, 0), min(b, i1 - i0)
        if b > a:
            covered[a:b] = True
    return int((~covered).sum())


def coverage_report(inst: pd.DataFrame, cur: pd.DataFrame, lc: pd.DataFrame, idm: pd.DataFrame, ih: pd.DataFrame,
                    cal: np.ndarray, sym: dict) -> tuple[pd.DataFrame, str]:
    ids = inst.tradingItemId.to_numpy(dtype=np.int64)
    l = lc.set_index("tradingItemId").reindex(ids)
    # trading days per item on the calendar (whole life and 2000+)
    first, last = l.firstTradeDate.to_numpy(), l.lastTradeDate.to_numpy()
    tdays = ml.tdays_inclusive(cal, first, last)
    start2000 = np.maximum(first, np.datetime64("2000-01-03", "ns"))
    tdays2000 = ml.tdays_inclusive(cal, start2000, last)
    per = pd.DataFrame({"tradingItemId": ids, "tdays": tdays, "tdays2000": tdays2000, "firstTradeDate": first, "lastTradeDate": last})
    per["symbol"] = per.tradingItemId.map(cur.set_index("tradingItemId").symbol)
    per["companyName"] = per.tradingItemId.map(inst.set_index("tradingItemId").companyName)

    mapped = set(idm.tradingItemId.unique())
    per["A_noMapping"] = ~per.tradingItemId.isin(mapped)
    # B: gaps inside the priced period
    groups = {tid: list(zip(g.symbolStartDate, g.symbolEndDate)) for tid, g in idm.groupby("tradingItemId")}
    gap = np.zeros(len(per), dtype=np.int64)
    for i, (tid, f, la) in enumerate(zip(per.tradingItemId, per.firstTradeDate, per.lastTradeDate)):
        iv = groups.get(int(tid))
        if iv:
            gap[i] = gap_days_in_period(cal, pd.Timestamp(f) if not pd.isna(f) else pd.NaT, pd.Timestamp(la) if not pd.isna(la) else pd.NaT, iv)
    per["B_gapDays"] = gap
    per["B_gap"] = per.B_gapDays > 0
    # C: conflicts (days of overlap inside the priced period)
    conf_ids = set(idm.loc[idm.conflictFlag, "tradingItemId"].unique())
    per["C_conflict"] = per.tradingItemId.isin(conf_ids)
    cdays = np.zeros(len(per), dtype=np.int64)
    for i, (tid, f, la) in enumerate(zip(per.tradingItemId, per.firstTradeDate, per.lastTradeDate)):
        if int(tid) not in conf_ids or pd.isna(f) or pd.isna(la):
            continue
        g = idm[(idm.tradingItemId == tid)]
        s = g.symbolStartDate.astype("datetime64[ms]").fillna(NEG_INF).to_numpy()
        e = g.symbolEndDate.astype("datetime64[ms]").fillna(POS_INF).to_numpy()
        key = (g.gvkey.fillna("") + "|" + g.iid.fillna("")).to_numpy()
        days = 0
        for a in range(len(g)):
            for b in range(a + 1, len(g)):
                if key[a] == key[b]:
                    continue
                lo = max(s[a], s[b], np.datetime64(f, "ms"))
                hi = min(e[a], e[b], np.datetime64(la, "ms"))
                if lo <= hi:
                    days = max(days, int(ml.tdays_inclusive(cal, np.array([lo]).astype("datetime64[ns]"), np.array([hi]).astype("datetime64[ns]"))[0]))
        cdays[i] = days
    per["C_conflictDays"] = cdays
    # D: mapping but no TIC/EXCHG history
    has_hist = set(ih.loc[ih.item.isin(["TIC", "EXCHG"]), "tradingItemId"].unique())
    per["D_noTicExchg"] = per.tradingItemId.isin(mapped) & ~per.tradingItemId.isin(has_hist)

    top500, top1000 = universe_members("Top500"), universe_members("Top1000")
    per["everTop500"] = per.tradingItemId.isin(top500)
    per["everTop1000"] = per.tradingItemId.isin(top1000)
    per.to_parquet(mb.QA_DIR / "id_mapping_coverage_per_item.parquet", **mb.PARQUET_KW)

    def block(mask_name: str, sel: pd.Series, day_col: str, days_col_for_class: str | None = None) -> dict:
        d = per[sel]
        n_items = int(d[mask_name].sum())
        days = int(d.loc[d[mask_name], days_col_for_class if days_col_for_class else day_col].sum())
        return {"items": n_items, "items_pct": round(100 * n_items / max(len(d), 1), 2), "days": days,
                "days_pct": round(100 * days / max(int(d[day_col].sum()), 1), 3)}

    scopes = {"ALL": per.tradingItemId.notna(), "everTop500": per.everTop500, "everTop1000": per.everTop1000}
    table = {}
    for scope, sel in scopes.items():
        day_col = "tdays2000" if scope != "ALL" else "tdays"
        table[scope] = {
            "n_items": int(sel.sum()), "trading_days": int(per.loc[sel, day_col].sum()),
            "A_no_mapping": block("A_noMapping", sel, day_col),
            "B_gap_in_priced_period": block("B_gap", sel, day_col, "B_gapDays"),
            "C_same_day_conflict": block("C_conflict", sel, day_col, "C_conflictDays"),
            "D_no_TIC_EXCHG_history": block("D_noTicExchg", sel, day_col),
        }
    mb.save_json(table, mb.QA_DIR / "id_mapping_coverage.json")

    lines = ["# ID 매핑 커버리지 (ciqgvkeyiid → tradingItemId, sec_idhist TIC/EXCHG)", "",
             f"- 기준: 전체 {len(per):,} 종목, everTop500 {int(per.everTop500.sum()):,}, everTop1000 {int(per.everTop1000.sum()):,} "
             f"(프로토타입 TopUniverseV2 Top500/Top1000에서 한 번이라도 True). 거래일 = ciq 거래 캘린더 기준 [firstTradeDate, lastTradeDate] 일수"
             f"(ALL은 전 생애, 유니버스 기준은 2000-01-03 이후).", "",
             "| 분류 | 범위 | 종목 수 | 종목 % | 거래일(해당 분류) | 거래일 % |", "|---|---|---|---|---|---|"]
    names = {"A_no_mapping": "A. 매핑 없음", "B_gap_in_priced_period": "B. 가격 기간 중 매핑 공백(공백일수)",
             "C_same_day_conflict": "C. 같은 날 복수 gvkey/iid(충돌일수)", "D_no_TIC_EXCHG_history": "D. 매핑 있으나 TIC/EXCHG 이력 없음"}
    for cls, nm in names.items():
        for scope in scopes:
            b = table[scope][cls]
            lines.append(f"| {nm} | {scope} ({table[scope]['n_items']:,} / {table[scope]['trading_days']:,}일) | {b['items']:,} | {b['items_pct']} | {b['days']:,} | {b['days_pct']} |")
    lines += ["", "## Top500 충돌 종목 (C)", ""]
    c5 = per[per.everTop500 & per.C_conflict].sort_values("C_conflictDays", ascending=False)
    if len(c5):
        lines += ["| tradingItemId | symbol | companyName | 충돌 거래일 | 매핑 행 |", "|---|---|---|---|---|"]
        for r in c5.itertuples():
            g = idm[idm.tradingItemId == r.tradingItemId]
            desc = "; ".join(f"{gv}/{ii} [{'' if pd.isna(s) else s.date()}..{'' if pd.isna(e) else e.date()}] act={a}"
                             for gv, ii, s, e, a in zip(g.gvkey, g.iid, g.symbolStartDate, g.symbolEndDate, g.activeFlag))
            lines.append(f"| {r.tradingItemId} | {r.symbol} | {r.companyName} | {r.C_conflictDays} | {desc} |")
    else:
        lines.append("없음")
    lines += ["", "## Top500 매핑 없음 / 공백 / TIC·EXCHG 이력 없음 (A·B·D) 상위", ""]
    for col, nm, dcol in (("A_noMapping", "A 매핑 없음", "tdays2000"), ("B_gap", "B 공백", "B_gapDays"), ("D_noTicExchg", "D 이력 없음", "tdays2000")):
        d = per[per.everTop500 & per[col]].sort_values(dcol, ascending=False).head(15)
        lines.append(f"### {nm} (Top500 {int((per.everTop500 & per[col]).sum())} 종목, 상위 15)")
        if len(d):
            lines += ["| tradingItemId | symbol | companyName | firstTradeDate | lastTradeDate | 일수 |", "|---|---|---|---|---|---|"]
            for r in d.itertuples():
                lines.append(f"| {r.tradingItemId} | {r.symbol} | {r.companyName} | {pd.Timestamp(r.firstTradeDate).date() if pd.notna(r.firstTradeDate) else ''} | "
                             f"{pd.Timestamp(r.lastTradeDate).date() if pd.notna(r.lastTradeDate) else ''} | {getattr(r, dcol)} |")
        else:
            lines.append("없음")
        lines.append("")
    # reverse conflicts: same (gvkey, iid) -> several tradingItemIds at the same time
    rev = []
    for (gv, ii), g in idm.groupby(["gvkey", "iid"]):
        if g.tradingItemId.nunique() < 2:
            continue
        s = g.symbolStartDate.astype("datetime64[ms]").fillna(NEG_INF).to_numpy()
        e = g.symbolEndDate.astype("datetime64[ms]").fillna(POS_INF).to_numpy()
        t = g.tradingItemId.to_numpy()
        for a in range(len(g)):
            for b in range(a + 1, len(g)):
                if t[a] != t[b] and max(s[a], s[b]) <= min(e[a], e[b]):
                    rev.append((gv, ii, int(t[a]), int(t[b]), str(max(s[a], s[b]))[:10], str(min(e[a], e[b]))[:10]))
    rev = pd.DataFrame(rev, columns=["gvkey", "iid", "idA", "idB", "fromDate", "toDate"]).drop_duplicates()
    rev.to_csv(mb.QA_DIR / "id_mapping_reverse_conflicts.csv", index=False)
    lines += [f"## 역방향 충돌 (같은 gvkey/iid가 같은 시기에 복수 tradingItemId): {len(rev)} 쌍 → `id_mapping_reverse_conflicts.csv`", ""]
    if len(rev):
        lines += ["| gvkey | iid | idA | idB | from | to |", "|---|---|---|---|---|---|"]
        for r in rev.head(25).itertuples():
            lines.append(f"| {r.gvkey} | {r.iid} | {r.idA} | {r.idB} | {r.fromDate} | {r.toDate} |")
    text = "\n".join(lines) + "\n"
    (mb.QA_DIR / "id_mapping_coverage.md").write_text(text)
    return per, text


# --------------------------------------------------------------------------------------
# symbol-set consistency (T2 shared symbol file, and the 8 master files against instruments)
# --------------------------------------------------------------------------------------
SHARED_SYMBOLS = mb.SCRATCH / "stage_v21" / "shared" / "symbols_v21.parquet"


def symbolset_check(inst: pd.DataFrame, cur: pd.DataFrame) -> dict:
    """Compare instruments with T2's shared symbol file (if present) -> qa/symbolset_diff.csv; and check that every
    master file's id set is the instruments set (current/lifecycle/classification/primary) or a subset
    (id_mapping/identifier_history/lineage.oldId)."""
    ids = set(int(i) for i in inst.tradingItemId)
    res = {"n_instruments": len(ids), "shared_file": str(SHARED_SYMBOLS), "shared_present": SHARED_SYMBOLS.exists()}
    if SHARED_SYMBOLS.exists():
        sh = pd.read_parquet(SHARED_SYMBOLS)
        col = "tradingItemId" if "tradingItemId" in sh.columns else sh.columns[0]
        t2 = set(int(i) for i in sh[col])
        only_t1 = sorted(ids - t2)
        only_t2 = sorted(t2 - ids)
        k = cur.set_index("tradingItemId")
        rows = [{"tradingItemId": i, "side": "T1_only", "symbol": k.symbol.get(i), "exchangeId": k.exchangeId.get(i)} for i in only_t1]
        rows += [{"tradingItemId": i, "side": "T2_only", "symbol": None, "exchangeId": None} for i in only_t2]
        pd.DataFrame(rows, columns=["tradingItemId", "side", "symbol", "exchangeId"]).to_csv(mb.QA_DIR / "symbolset_diff.csv", index=False)
        res.update({"n_shared": len(t2), "n_T1_only": len(only_t1), "n_T2_only": len(only_t2),
                    "shared_mtime": str(pd.Timestamp(SHARED_SYMBOLS.stat().st_mtime, unit="s"))[:19],
                    "shared_isNew_count": int(sh["isNew"].sum()) if "isNew" in sh.columns else None})
    else:
        pd.DataFrame(columns=["tradingItemId", "side", "symbol", "exchangeId"]).to_csv(mb.QA_DIR / "symbolset_diff.csv", index=False)
        res["status"] = "T2 대조 대기 (shared/symbols_v21.parquet 없음)"
    per_file = {}
    for name in ("current", "lifecycle", "classification_history", "primary_history"):
        s = set(int(i) for i in pd.read_parquet(mb.MASTER_DIR / f"{name}.parquet", columns=["tradingItemId"]).tradingItemId)
        per_file[name] = {"n_ids": len(s), "equals_instruments": s == ids, "missing": len(ids - s), "extra": len(s - ids)}
    for name in ("id_mapping", "identifier_history"):
        s = set(int(i) for i in pd.read_parquet(mb.MASTER_DIR / f"{name}.parquet", columns=["tradingItemId"]).tradingItemId)
        per_file[name] = {"n_ids": len(s), "subset_of_instruments": s <= ids, "extra": len(s - ids)}
    lin = pd.read_parquet(mb.MASTER_DIR / "lineage.parquet", columns=["oldId", "newId"])
    per_file["lineage"] = {"n_pairs": len(lin), "oldId_subset": set(int(i) for i in lin.oldId) <= ids,
                           "newId_outside_instruments": int((~lin.newId.isin(list(ids))).sum())}
    exp = mb.MASTER_DIR / "tradable_exclusions.parquet"
    if exp.exists():
        ex = pd.read_parquet(exp)
        per_file["tradable_exclusions"] = {"n_rows": len(ex), "subset_of_instruments": set(int(i) for i in ex.tradingItemId) <= ids,
                                           "preferred_subset": set(int(i) for i in ex.preferredItemId) <= ids}
    res["per_file"] = per_file
    res["all_files_consistent"] = all(v.get("equals_instruments", True) and v.get("subset_of_instruments", True) and v.get("oldId_subset", True)
                                      for v in per_file.values())
    return res


# --------------------------------------------------------------------------------------
# run all gates
# --------------------------------------------------------------------------------------
def run_all(ident: pd.DataFrame, cur: pd.DataFrame, idm: pd.DataFrame, cal: np.ndarray, as_of_price: pd.Timestamp, sym: dict) -> dict:
    inst = pd.read_parquet(mb.MASTER_DIR / "instruments.parquet")
    lc = pd.read_parquet(mb.MASTER_DIR / "lifecycle.parquet")
    ih = pd.read_parquet(mb.MASTER_DIR / "identifier_history.parquet")
    cls = pd.read_parquet(mb.MASTER_DIR / "classification_history.parquet")
    prim = pd.read_parquet(mb.MASTER_DIR / "primary_history.parquet")

    res = {"self_test_overlap_checker": self_test_overlap(), "self_test_same_security_checker": self_test_same_security(cal),
           "symbol_set": symbolset_check(inst, cur)}
    g1 = same_security_overlap(inst, lc, cal)
    g1 = g1.merge(cur[["tradingItemId", "symbol", "exchangeId"]].rename(columns={"tradingItemId": "idA", "symbol": "symbolA", "exchangeId": "exchA"}), on="idA", how="left")
    g1 = g1.merge(cur[["tradingItemId", "symbol", "exchangeId"]].rename(columns={"tradingItemId": "idB", "symbol": "symbolB", "exchangeId": "exchB"}), on="idB", how="left")
    g1 = g1.sort_values("overlapTradingDays", ascending=False).reset_index(drop=True)
    g1.to_csv(mb.QA_DIR / "gate_same_security_overlap.csv", index=False)
    res["G1_same_security_both_tradable"] = {"pairs": int(len(g1)), "total_overlap_days": int(g1.overlapTradingDays.sum()) if len(g1) else 0,
                                             "top": g1.head(20).to_dict(orient="records")}
    # G1 after tradable_exclusions: an excluded item is not tradable inside [fromDate, toDate]
    exp = mb.MASTER_DIR / "tradable_exclusions.parquet"
    if exp.exists():
        ex = pd.read_parquet(exp)
        left = []
        for r in g1.itertuples():
            lo, hi = pd.Timestamp(r.overlapFrom), pd.Timestamp(r.overlapTo)
            covered = ex[ex.tradingItemId.isin([r.idA, r.idB]) & (ex.fromDate <= lo) & (ex.toDate >= hi)]
            if covered.empty:
                left.append({"securityId": r.securityId, "idA": r.idA, "idB": r.idB, "overlapTradingDays": r.overlapTradingDays})
        res["G1_after_exclusions"] = {"pairs_left": len(left), "days_left": int(sum(x["overlapTradingDays"] for x in left)), "left": left,
                                      "exclusion_rows": int(len(ex)), "excluded_days_total": int(ml.tdays_inclusive(cal, ex.fromDate.to_numpy(), ex.toDate.to_numpy()).sum()) if len(ex) else 0}
    # lineage same-line residual overlap after the firstTradeDate lift
    fd = mb.load_json(mb.QA_DIR / "lifecycle_first_date_summary.json", {})
    if fd:
        res["G1b_lineage_same_line_after_lift"] = {k: fd.get(k) for k in ("pairs_window_overlap", "pairs_stitched_copy", "stitch_test_counts", "pairs_not_lifted",
                                                                            "pairs_role_swapped", "new_ids_adjusted", "by_source", "skipped_first_after_last",
                                                                            "gap_days_between_old_and_new_total", "gap_days_gt0_ids",
                                                                            "residual_same_line_overlap_pairs", "residual_same_line_overlap_days", "residual_top")}
        # residual same-line overlap by category (securityId-only pairs are covered by tradable_exclusions)
        rp = mb.QA_DIR / "lineage_residual_overlap.csv"
        if rp.exists():
            rr = pd.read_csv(rp)
            lf = pd.read_parquet(mb.WORK / "lineage_full.parquet").set_index(["oldId", "newId"])
            rr["sources"] = [lf.sources.get((a, b)) for a, b in zip(rr.oldId, rr.newId)]
            exp = mb.MASTER_DIR / "tradable_exclusions.parquet"
            exd = pd.read_parquet(exp) if exp.exists() else pd.DataFrame(columns=["tradingItemId", "fromDate", "toDate"])
            cov = []
            for r in rr.itertuples():
                lo, hi = pd.Timestamp(r.overlapFrom), pd.Timestamp(r.overlapTo)
                cov.append(bool((exd.tradingItemId.isin([r.oldId, r.newId]) & (exd.fromDate <= lo) & (exd.toDate >= hi)).any()))
            rr["coveredByExclusion"] = cov
            res["G1b_lineage_same_line_after_lift"]["residual_by_source"] = rr.groupby("sources").agg(pairs=("oldId", "size"), days=("overlapTradingDays", "sum")).to_dict(orient="index")
            res["G1b_lineage_same_line_after_lift"]["residual_not_covered_by_exclusions"] = {"pairs": int((~rr.coveredByExclusion).sum()),
                                                                                              "days": int(rr.loc[~rr.coveredByExclusion, "overlapTradingDays"].sum())}
    o_ih = overlaps_within(ih, "tradingItemId", "effFrom", "effThru", ["item", "value"])
    o_ih_any = overlaps_within(ih, "tradingItemId", "effFrom", "effThru", ["item"])
    o_cls = overlaps_within(cls, "tradingItemId", "effectiveFrom", "effectiveTo")
    o_prim = overlaps_within(prim, "tradingItemId", "effectiveFrom", "effectiveTo")
    o_idm = overlaps_within(idm[~idm.conflictFlag], "tradingItemId", "symbolStartDate", "symbolEndDate")
    res["G2_interval_overlaps"] = {
        "identifier_history_same_item_same_value": int(len(o_ih)),
        "identifier_history_same_item_any_value": int(len(o_ih_any)),
        "identifier_history_same_item_any_value_by_quality": ih.loc[ih.index.isin(o_ih_any._row), "mappingQuality"].value_counts().to_dict() if len(o_ih_any) else {},
        "classification_history": int(len(o_cls)), "primary_history": int(len(o_prim)),
        "id_mapping_nonconflict_rows": int(len(o_idm)),
    }
    o_ih_any.to_csv(mb.QA_DIR / "gate_identifier_history_overlaps.csv", index=False)
    # coverage
    per, _ = coverage_report(inst, cur, lc, idm, ih, cal, sym)
    res["coverage_written"] = str(mb.QA_DIR / "id_mapping_coverage.md")
    mb.save_json(res, mb.QA_DIR / "gates.json")
    log.info("gates: G1 pairs=%d, G2=%s, self-tests %s / %s", len(g1), res["G2_interval_overlaps"],
             res["self_test_overlap_checker"]["pass"], res["self_test_same_security_checker"]["pass"])
    return res


# --------------------------------------------------------------------------------------
# rc2 / R3: lineage v2 gates and aux coverage
# --------------------------------------------------------------------------------------
LINEAGE_RELATIONS = {"rename", "relist", "merger", "spinoff_parent", "unknown"}
SAME_LINE = {"rename", "relist"}


def lineage_v2_self_test(cal: np.ndarray) -> dict:
    """A clean same-line pair (old ends before new starts) yields no overlap; a planted pair (new starts while old still trades)
    is reported -> the detector can fail."""
    lin = pd.DataFrame({"oldId": [1, 3], "newId": [2, 4], "relation": ["rename", "relist"], "confidence": ["high", "medium"],
                        "ratio": [1.0, 0.5], "effectiveDate": pd.to_datetime(["2015-01-05", "2016-01-05"])})
    lc = pd.DataFrame({"tradingItemId": [1, 2, 3, 4],
                       "firstTradeDate": pd.to_datetime(["2010-01-04", "2015-01-05", "2010-01-04", "2015-06-01"]),
                       "lastTradeDate": pd.to_datetime(["2015-01-02", "2020-01-02", "2016-01-04", "2020-01-02"])})   # 3/4 overlap Jun 2015 - Jan 2016
    r = same_line_overlap(lin, lc, cal)
    return {"clean_pairs": int(((r.oldId == 1) & (r.newId == 2)).sum()), "planted_pairs": int(((r.oldId == 3) & (r.newId == 4)).sum()),
            "pass": bool(((r.oldId == 1).sum() == 0) and ((r.oldId == 3).sum() == 1))}


def same_line_overlap(lineage: pd.DataFrame, lifecycle: pd.DataFrame, cal: np.ndarray, confidences=("high", "medium")) -> pd.DataFrame:
    """Same-line pairs (rename/relist, confidence in `confidences`) whose lifecycle windows [firstTradeDate, lastTradeDate] intersect
    = both ids tradable on the same day (the 'no interval overlap' gate)."""
    k = lifecycle.set_index("tradingItemId")
    rows = []
    sel = lineage[lineage.relation.isin(SAME_LINE) & lineage.confidence.isin(list(confidences))]
    for r in sel.itertuples():
        fo, lo = k.firstTradeDate.get(r.oldId, pd.NaT), k.lastTradeDate.get(r.oldId, pd.NaT)
        fn, ln = k.firstTradeDate.get(r.newId, pd.NaT), k.lastTradeDate.get(r.newId, pd.NaT)
        if pd.isna(fo) or pd.isna(lo) or pd.isna(fn) or pd.isna(ln):
            continue
        a, b = max(fo, fn), min(lo, ln)
        if a <= b:
            rows.append({"oldId": int(r.oldId), "newId": int(r.newId), "relation": r.relation, "confidence": r.confidence, "ratio": r.ratio,
                         "overlapFrom": a, "overlapTo": b, "overlapTradingDays": int(ml.tdays_inclusive(cal, np.array([a]), np.array([b]))[0])})
    return pd.DataFrame(rows, columns=["oldId", "newId", "relation", "confidence", "ratio", "overlapFrom", "overlapTo", "overlapTradingDays"])


def lineage_v2_gates(lineage: pd.DataFrame, lifecycle: pd.DataFrame, cal: np.ndarray, exclusions: pd.DataFrame | None = None,
                     out_dir: Path | None = None) -> dict:
    """Contract + overlap gates for master/lineage.parquet v2.  Returns the dict written to <out_dir>/lineage_v2_gates.json;
    the dual-tradable pair list goes to <out_dir>/lineage_v2_dual_tradable.csv."""
    res = {"self_test": lineage_v2_self_test(cal)}
    dup = int(lineage.duplicated(["oldId", "newId"]).sum())
    self_pairs = int((lineage.oldId == lineage.newId).sum())
    bad_rel = sorted(set(lineage.relation) - LINEAGE_RELATIONS)
    bad_conf = sorted(set(lineage.confidence) - {"high", "medium", "low"})
    bad_ratio = int((lineage.ratio.notna() & ~(lineage.ratio > 0)).sum())
    v2 = lineage[lineage.get("version", pd.Series("", index=lineage.index)).astype(str).str.startswith("v2")] if "version" in lineage.columns else lineage.iloc[0:0]
    res["contract"] = {"rows": int(len(lineage)), "duplicate_pairs": dup, "self_pairs": self_pairs, "unknown_relation_values": bad_rel,
                       "unknown_confidence_values": bad_conf, "non_positive_ratio": bad_ratio,
                       "v2_rows": int(len(v2)), "v2_rows_without_ratioSource": int((v2.ratioSource.fillna("") == "").sum()) if len(v2) else 0,
                       "v2_rows_without_linkClass": int((v2.linkClass.fillna("") == "").sum()) if len(v2) else 0,
                       "same_line_pairs": int(lineage.relation.isin(SAME_LINE).sum()),
                       "same_line_ratio_known": int((lineage.relation.isin(SAME_LINE) & lineage.ratio.notna()).sum()),
                       "pass": dup == 0 and self_pairs == 0 and not bad_rel and not bad_conf and bad_ratio == 0}
    ov = same_line_overlap(lineage, lifecycle, cal)
    if exclusions is not None and len(exclusions) and len(ov):
        cov = []
        for r in ov.itertuples():
            cov.append(bool((exclusions.tradingItemId.isin([r.oldId, r.newId]) & (exclusions.fromDate <= r.overlapFrom) & (exclusions.toDate >= r.overlapTo)).any()))
        ov["coveredByExclusion"] = cov
    else:
        ov["coveredByExclusion"] = False
    res["dual_tradable"] = {"pairs": int(len(ov)), "days": int(ov.overlapTradingDays.sum()) if len(ov) else 0,
                            "pairs_not_covered_by_exclusions": int((~ov.coveredByExclusion).sum()) if len(ov) else 0,
                            "by_confidence": ov.confidence.value_counts().to_dict() if len(ov) else {},
                            "top": ov.sort_values("overlapTradingDays", ascending=False).head(10).to_dict(orient="records")}
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        ov.to_csv(out_dir / "lineage_v2_dual_tradable.csv", index=False)
        mb.save_json(res, out_dir / "lineage_v2_gates.json")
    return res


def aux_coverage_check(instruments: pd.DataFrame, aux: pd.DataFrame, lineage: pd.DataFrame, events: pd.DataFrame | None,
                       ledger: pd.DataFrame | None, base_aux_ids: set[int], lifecycle_aux: pd.DataFrame, out_dir: Path | None = None) -> dict:
    """Every id a link/leg refers to is either in the symbol set or an aux instrument; aux prices/lifecycle rows match the aux list;
    aux ids never collide with the symbol set."""
    inset = set(int(i) for i in instruments.tradingItemId)
    auxset = set(int(i) for i in aux.tradingItemId)
    refs = set(int(i) for i in lineage.newId) | set(int(i) for i in lineage.oldId)
    n_lin_missing = sorted(refs - inset - auxset)
    ev_missing, led_missing = [], []
    if events is not None and len(events):
        s = events.successorTradingItemId.dropna().astype("int64")
        ev_missing = sorted(set(s) - inset - auxset)
    if ledger is not None and len(ledger):
        s = ledger.childTradingItemId.dropna().astype("int64")
        led_missing = sorted(set(s) - inset - auxset)
    usd = set(int(i) for i in aux.loc[aux.priceFetch == "usd", "tradingItemId"])
    res = {"n_instruments": len(inset), "n_aux": len(auxset), "aux_in_symbol_set": len(auxset & inset),
           "lineage_ids_uncovered": n_lin_missing[:20], "n_lineage_ids_uncovered": len(n_lin_missing),
           "events_successors_uncovered": ev_missing[:20], "n_events_successors_uncovered": len(ev_missing),
           "ledger_children_uncovered": led_missing[:20], "n_ledger_children_uncovered": len(led_missing),
           "aux_usd": len(usd), "base_aux_ids": len(base_aux_ids), "base_aux_not_in_aux": len(base_aux_ids - auxset),
           "aux_usd_without_prices": len(usd - base_aux_ids),
           "lifecycle_aux_rows": int(len(lifecycle_aux)), "lifecycle_aux_ids_match": set(int(i) for i in lifecycle_aux.tradingItemId) == auxset,
           "lifecycle_aux_status_all_aux": bool((lifecycle_aux.status == "aux").all()) if len(lifecycle_aux) else True}
    res["pass"] = (res["aux_in_symbol_set"] == 0 and not n_lin_missing and not ev_missing and not led_missing
                   and res["base_aux_not_in_aux"] == 0 and res["lifecycle_aux_ids_match"] and res["lifecycle_aux_status_all_aux"])
    if out_dir is not None:
        mb.save_json(res, out_dir / "aux_coverage_gate.json")
    return res
