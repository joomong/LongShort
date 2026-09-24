#!/usr/bin/env python3
"""cache_v21/master_lineage.py - T1: `lineage` (oldId -> newId) from three sources.

  (a) same securityId, several trading items in the symbol set (rename / relist: reincorporation, exchange move,
      re-listing).  relation 'rename' when the ticker changed on the same exchange, else 'relist'.
  (b) same Compustat (gvkey, iid) mapped to different tradingItemIds over time (ciqgvkeyiid periods) -> 'relist'.
  (c) events.successorTradingItemId -> 'merger' (ratio = successorRatio, evidence = actionText[:120]);
      actionCode N/C with a successor -> 'rename', SPN -> 'spinoff_parent'.

Ordering: chains are ordered by mapping period (b) or lastTradeDate (a) and only consecutive items form a pair;
oldId = the earlier item.  effectiveDate = event endDate for merger/spinoff, else the new item's ciqgvkeyiid
symbolStartDate, else the event endDate, else old lastTradeDate + 1 day.  confidence 'high' when two sources agree
(or a parsed merger with ratio), 'medium' for one dated source, 'low' otherwise.  ratio is NaN unless an event
states it.  Overlapping price histories (ciq stitched the past onto the new id) are measured exactly: common dates
with a non-null close in ciqpriceequity for both ids (two-id index scans), written to `qa/lineage_candidates.csv`
with the overlap in days (overlapKind 'stitched_same_line' for rename/relist, 'coexist_two_companies' for mergers).
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from db import DB, SCHEMA  # noqa: E402
import master_build as mb  # noqa: E402
import master_lifecycle as ml  # noqa: E402

log = logging.getLogger("cache_v21.lineage")

LINEAGE_COLS = ["oldId", "newId", "relation", "effectiveDate", "ratio", "evidence", "confidence"]
MUST_CATCH = [(24687598, 618650356), (6196269, 613560578), (613560578, 1875166722), (2663922, 569484256),
              (2623229, 372277281), (2592597, 433925520)]   # + RIG 51757943 (single id; noted, no pair)


def _order(a: int, b: int, lc: pd.DataFrame) -> tuple[int, int]:
    """old, new by lastTradeDate (then firstTradeDate, then id)."""
    la, lb = lc.lastTradeDate.get(a, pd.NaT), lc.lastTradeDate.get(b, pd.NaT)
    fa, fb = lc.firstTradeDate.get(a, pd.NaT), lc.firstTradeDate.get(b, pd.NaT)
    ka = (pd.Timestamp(la) if pd.notna(la) else pd.Timestamp("2900-01-01"), pd.Timestamp(fa) if pd.notna(fa) else pd.Timestamp("2900-01-01"), a)
    kb = (pd.Timestamp(lb) if pd.notna(lb) else pd.Timestamp("2900-01-01"), pd.Timestamp(fb) if pd.notna(fb) else pd.Timestamp("2900-01-01"), b)
    return (a, b) if ka <= kb else (b, a)


def candidates_security(ident: pd.DataFrame, cur: pd.DataFrame, lc: pd.DataFrame) -> list[dict]:
    k = cur.set_index("tradingItemId")
    rows = []
    for sid, g in ident.groupby("securityid"):
        if len(g) < 2:
            continue
        tids = list(g.tradingitemid.astype(int))
        # chain order by (lastTradeDate, firstTradeDate); only consecutive items form a pair
        tids.sort(key=lambda t: (lc.lastTradeDate.get(t, pd.NaT) if pd.notna(lc.lastTradeDate.get(t, pd.NaT)) else pd.Timestamp("2900-01-01"),
                                 lc.firstTradeDate.get(t, pd.NaT) if pd.notna(lc.firstTradeDate.get(t, pd.NaT)) else pd.Timestamp("2900-01-01"), t))
        for old, new in zip(tids[:-1], tids[1:]):
            so, sn = k.symbol.get(old), k.symbol.get(new)
            eo, en = k.exchangeId.get(old), k.exchangeId.get(new)
            same_exch = pd.notna(eo) and pd.notna(en) and int(eo) == int(en)
            rel = "rename" if (same_exch and str(so) != str(sn)) else "relist"
            rows.append({"oldId": old, "newId": new, "relation": rel, "src": "securityId",
                         "evidence": f"same securityId {int(sid)}: {so}@{eo} -> {sn}@{en}", "ratio": np.nan})
    return rows


def candidates_gvkey(id_mapping: pd.DataFrame, lc: pd.DataFrame, ids: set[int]) -> list[dict]:
    rows = []
    m = id_mapping[id_mapping.tradingItemId.isin(ids)]
    for (gv, iid), g in m.groupby(["gvkey", "iid"]):
        if g.tradingItemId.nunique() < 2:
            continue
        # one period per tradingItemId (earliest start / latest end), chain by period start; consecutive pairs only
        per = g.groupby("tradingItemId").agg(start=("symbolStartDate", "min"), end=("symbolEndDate", "max")).reset_index()
        per["_s"] = per.start.fillna(pd.Timestamp("1800-01-01"))
        per["_e"] = per.end.fillna(pd.Timestamp("2200-01-01"))
        per = per.sort_values(["_s", "_e", "tradingItemId"]).reset_index(drop=True)
        for i in range(len(per) - 1):
            po, pn = per.iloc[i], per.iloc[i + 1]
            old, new = int(po.tradingItemId), int(pn.tradingItemId)
            rows.append({"oldId": old, "newId": new, "relation": "relist", "src": "gvkeyiid", "ratio": np.nan,
                         "newStart": pn.start, "oldEnd": po.end,
                         "evidence": f"same gvkey/iid {gv}/{iid}: old period ..{'' if pd.isna(po.end) else po.end.date()}, "
                                     f"new period {'' if pd.isna(pn.start) else pn.start.date()}.."})
    return rows


def candidates_events(events: pd.DataFrame, ids: set[int]) -> list[dict]:
    rows = []
    e = events[events.successorTradingItemId.notna()].copy()
    e["succ"] = pd.to_numeric(e.successorTradingItemId, errors="coerce")
    e = e[e.succ.notna()]
    n_self = 0
    for r in e.itertuples():
        old, new = int(r.tradingItemId), int(r.succ)
        if old not in ids:
            continue
        if old == new:                 # events file names the item itself as successor (generator artifact) -> not a lineage edge
            n_self += 1
            continue
        code = r.actionCode if isinstance(r.actionCode, str) else ""
        rel = "merger" if code in ("ACQ", "MGD", "") else ("rename" if code == "N/C" else ("spinoff_parent" if code == "SPN" else "unknown"))
        if code in ("LIQ", "BK11", "BKR", "PVT", "SEC", "OTH", "DFF"):
            rel = "merger" if pd.notna(r.successorRatio) else "unknown"
        txt = (r.actionText or "")[:120] if isinstance(r.actionText, str) else ""
        rows.append({"oldId": old, "newId": new, "relation": rel, "src": "events", "ratio": float(r.successorRatio) if pd.notna(r.successorRatio) else np.nan,
                     "eventDate": r.endDate, "evidence": f"events[{code or 'NA'}] {txt}", "eventConfidence": getattr(r, "confidence", ""),
                     "successorInSet": new in ids})
    if n_self:
        log.warning("events: %d rows have successorTradingItemId == tradingItemId (skipped)", n_self)
    candidates_events.n_self = n_self
    return rows


def overlap_days(db: DB, pairs: list[tuple[int, int]]) -> dict[tuple[int, int], tuple[int, str, str, int]]:
    """Exact count of dates with a non-null close in ciqpriceequity for both ids (index scans on 2 ids), plus the number
    of those dates on which the as-quoted closes (priceclose x adjustmentfactor) agree within 0.1% -> a stitched copy
    shows ~100% equal closes, two genuinely different lines do not."""
    out = mb.WORK / "lineage_overlap.parquet"
    cache = {}
    if out.exists():
        c = pd.read_parquet(out)
        if "n_eq" in c.columns:
            cache = {(int(a), int(b)): (int(n), str(f), str(t), int(e)) for a, b, n, f, t, e in
                     zip(c["oldId"], c["newId"], c["n"], c["first"], c["last"], c["n_eq"])}
    todo = [p for p in pairs if p not in cache]
    t0 = time.time()
    for a, b in todo:
        r = db.query(f"""
            select count(*) n, min(a.pricingdate) f, max(a.pricingdate) t,
                   count(*) filter (where abs(a.priceclose * a.adjustmentfactor - b.priceclose * b.adjustmentfactor)
                                          <= 0.001 * greatest(a.priceclose * a.adjustmentfactor, 1e-9)) n_eq
            from {SCHEMA}.ciqpriceequity a join {SCHEMA}.ciqpriceequity b
              on b.tradingitemid = %s and b.pricingdate = a.pricingdate and b.priceclose is not null
            where a.tradingitemid = %s and a.priceclose is not null""", (int(b), int(a)))
        n = int(r.n.iloc[0]) if len(r) else 0
        cache[(a, b)] = (n, str(r.f.iloc[0]) if n else "", str(r.t.iloc[0]) if n else "", int(r.n_eq.iloc[0] or 0) if len(r) else 0)
    if todo:
        log.info("overlap counts for %d pairs (%.0fs)", len(todo), time.time() - t0)
        pd.DataFrame([(a, b, v[0], v[1], v[2], v[3]) for (a, b), v in cache.items()],
                     columns=["oldId", "newId", "n", "first", "last", "n_eq"]).to_parquet(out, **mb.PARQUET_KW)
    return cache


def step_lineage(db: DB, ident: pd.DataFrame, cur: pd.DataFrame, id_mapping: pd.DataFrame, cal: np.ndarray) -> pd.DataFrame:
    lc = pd.read_parquet(mb.MASTER_DIR / "lifecycle.parquet").set_index("tradingItemId")
    events, ev_version = ml.load_events()
    ids = set(int(i) for i in ident.tradingitemid)
    rows = candidates_security(ident, cur, lc) + candidates_gvkey(id_mapping, lc, ids) + candidates_events(events, ids)
    cand = pd.DataFrame(rows)
    log.info("lineage candidates: %d rows (%s)", len(cand), cand.src.value_counts().to_dict() if len(cand) else {})

    # one row per (oldId, newId): merge sources
    keyed = {}
    for r in cand.to_dict(orient="records"):
        key = (int(r["oldId"]), int(r["newId"]))
        rev = (key[1], key[0])
        if rev in keyed and key not in keyed:       # events may order the pair differently from price dates
            key = rev
        cur_r = keyed.setdefault(key, {"oldId": key[0], "newId": key[1], "srcs": [], "relations": [], "evidence": [], "ratio": np.nan,
                                       "newStart": pd.NaT, "eventDate": pd.NaT, "eventConfidence": ""})
        cur_r["srcs"].append(r["src"])
        cur_r["relations"].append(r["relation"])
        cur_r["evidence"].append(r["evidence"])
        if pd.notna(r.get("ratio", np.nan)):
            cur_r["ratio"] = r["ratio"]
        if pd.notna(r.get("newStart", pd.NaT)):
            cur_r["newStart"] = r["newStart"]
        if pd.notna(r.get("eventDate", pd.NaT)):
            cur_r["eventDate"] = r["eventDate"]
        if r.get("eventConfidence"):
            cur_r["eventConfidence"] = r["eventConfidence"]
    merged = []
    for key, r in keyed.items():
        srcs = sorted(set(r["srcs"]))
        rel_pri = ["merger", "spinoff_parent", "rename", "relist", "unknown"]
        rels = sorted(set(r["relations"]), key=rel_pri.index)
        relation = rels[0]
        if len(srcs) >= 2:
            conf = "high"                                     # independent sources agree
        elif srcs == ["events"]:
            conf = "high" if (r["eventConfidence"] in ("parsed", "override") or pd.notna(r["ratio"])) and relation == "merger" else "medium"
        elif srcs == ["gvkeyiid"]:
            conf = "medium" if pd.notna(r["newStart"]) else "low"
        else:                                                 # securityId only
            conf = "medium"
        old_last = lc.lastTradeDate.get(key[0], pd.NaT)
        if relation in ("merger", "spinoff_parent") and pd.notna(r["eventDate"]):
            eff, eff_src = r["eventDate"], "event endDate"
        elif pd.notna(r["newStart"]):
            eff, eff_src = r["newStart"], "new id symbolStartDate"
        elif pd.notna(r["eventDate"]):
            eff, eff_src = r["eventDate"], "event endDate"
        elif pd.notna(old_last):
            eff, eff_src = pd.Timestamp(old_last) + pd.Timedelta(days=1), "old lastTradeDate+1"
        else:
            eff, eff_src = pd.NaT, "unknown"
        merged.append({"oldId": key[0], "newId": key[1], "relation": relation, "effectiveDate": eff, "ratio": r["ratio"],
                       "evidence": (" || ".join(r["evidence"]))[:400] + f" | effectiveDate from {eff_src}", "confidence": conf,
                       "sources": "+".join(srcs)})
    lin = pd.DataFrame(merged) if merged else pd.DataFrame(columns=LINEAGE_COLS + ["sources"])

    # exact overlap of close histories for every pair whose both ids are in the set
    pairs = [(int(a), int(b)) for a, b in zip(lin.oldId, lin.newId) if int(a) in ids and int(b) in ids]
    ov = overlap_days(db, pairs)
    lin["overlapDays"] = [ov[(int(a), int(b))][0] if (int(a), int(b)) in ov else np.nan for a, b in zip(lin.oldId, lin.newId)]
    lin["overlapFirst"] = [ov.get((int(a), int(b)), (0, "", "", 0))[1] for a, b in zip(lin.oldId, lin.newId)]
    lin["overlapLast"] = [ov.get((int(a), int(b)), (0, "", "", 0))[2] for a, b in zip(lin.oldId, lin.newId)]
    lin["overlapEqualDays"] = [ov[(int(a), int(b))][3] if (int(a), int(b)) in ov else np.nan for a, b in zip(lin.oldId, lin.newId)]
    lin["equalShare"] = lin.overlapEqualDays / lin.overlapDays.where(lin.overlapDays > 0)
    k = cur.set_index("tradingItemId")
    for side in ("old", "new"):
        col = f"{side}Id"
        lin[f"{side}Symbol"] = lin[col].map(k.symbol)
        lin[f"{side}Exchange"] = lin[col].map(k.exchangeId)
        lin[f"{side}First"] = lin[col].map(lc.firstTradeDate)
        lin[f"{side}Last"] = lin[col].map(lc.lastTradeDate)
        lin[f"{side}SecEnd"] = lin[col].map(lc.securityEndDate)
    lin["newInSet"] = lin.newId.isin(ids)
    lin["overlapKind"] = np.where(lin.relation.isin(["rename", "relist"]), "stitched_same_line", "coexist_two_companies")
    lin = lin.sort_values(["overlapDays", "oldId"], ascending=[False, True]).reset_index(drop=True)
    lin.to_csv(mb.QA_DIR / "lineage_candidates.csv", index=False)
    lin.to_parquet(mb.WORK / "lineage_full.parquet", **mb.PARQUET_KW)      # consumed by master_lifecycle.apply_lineage_first_dates

    out = lin[LINEAGE_COLS].copy()
    out["oldId"] = out.oldId.astype("int64")
    out["newId"] = out.newId.astype("int64")
    out["effectiveDate"] = mb.to_ns(out.effectiveDate)
    out["ratio"] = out.ratio.astype("float64")
    out = out.sort_values(["oldId", "newId"]).reset_index(drop=True)
    mb.write_parquet(out, mb.MASTER_DIR / "lineage.parquet")

    must = {p: ((lin.oldId == p[0]) & (lin.newId == p[1])).any() or ((lin.oldId == p[1]) & (lin.newId == p[0])).any() for p in MUST_CATCH}
    summary = {"n_candidate_rows": int(len(cand)), "by_source": cand.src.value_counts().to_dict() if len(cand) else {},
               "events_self_successor_skipped": int(getattr(candidates_events, "n_self", 0)),
               "n_pairs": int(len(lin)), "by_relation": out.relation.value_counts().to_dict(), "by_confidence": out.confidence.value_counts().to_dict(),
               "by_sources": lin.sources.value_counts().to_dict(), "pairs_with_overlap_gt0": int((lin.overlapDays.fillna(0) > 0).sum()),
               "pairs_overlap_gt250": int((lin.overlapDays.fillna(0) > 250).sum()), "ratio_known": int(out.ratio.notna().sum()),
               "same_line_pairs": int(lin.relation.isin(["rename", "relist"]).sum()),
               "same_line_pairs_overlap_gt0": int((lin.relation.isin(["rename", "relist"]) & (lin.overlapDays.fillna(0) > 0)).sum()),
               "same_line_pairs_overlap_gt250": int((lin.relation.isin(["rename", "relist"]) & (lin.overlapDays.fillna(0) > 250)).sum()),
               "same_line_pairs_stitched_copy": int((lin.relation.isin(["rename", "relist"]) & (lin.overlapDays.fillna(0) > 0) & (lin.equalShare >= 0.9)).sum()),
               "same_line_pairs_prices_differ": int((lin.relation.isin(["rename", "relist"]) & (lin.overlapDays.fillna(0) > 0) & (lin.equalShare < 0.9)).sum()),
               "top_overlap_same_line": lin[lin.relation.isin(["rename", "relist"])].head(15)[["oldId", "oldSymbol", "newId", "newSymbol", "relation", "overlapDays", "overlapEqualDays", "overlapFirst", "overlapLast", "sources"]].to_dict(orient="records"),
               "must_catch": {f"{a}->{b}": bool(v) for (a, b), v in must.items()},
               "top_overlap": lin.head(15)[["oldId", "oldSymbol", "newId", "newSymbol", "relation", "overlapDays", "overlapFirst", "overlapLast", "sources"]].to_dict(orient="records"),
               "events_source": ev_version}
    mb.save_json(summary, mb.QA_DIR / "lineage_summary.json")
    log.info("lineage: %d pairs %s; must-catch %s", len(out), summary["by_relation"], summary["must_catch"])
    return out


# ======================================================================================
# rc2 / R3: reclassification of the 1:1-rename rejections -> lineage v2
# ======================================================================================
# Every `rejected:rename_not_1to1` events row (T6) is an N/C succession found through a shared Compustat (gvkey, iid)
# whose assumed 1:1 continuation moved the mark by more than 20%.  P12: a price jump alone never rejects a link; the
# link is classified from structural evidence (co_acthist text, ciqsplit rows at the transition, companyId/securityId,
# ciq's own stitched copy of the old prices onto the new id) and the exchange ratio comes from a dated source:
#   ciqsplit      split/consolidation rows (either id) with old lastClose < exDate <= new firstClose
#   co_acthist    "1-FOR-N REVERSE ..." / "X-FOR-1 STK SPLIT" / "N SHARES OF <new> FOR EVERY SHARE" in the N/C text
#   manual        public deal terms for a few large names (marked [추정], isEstimate) - never a price-implied number
#   assumed_1to1  no ratio evidence and no contradicting structure (ciq stitched copy / same company) -> 1.0
#   unresolved    terms unknown and 1:1 contradicted by the transition -> ratio NaN, price-implied value in reviewFlag
# linkClass in {simple_rename, relist, de_spac, merger, spin_off, mismatch}; relation keeps the v1 vocabulary
# ('rename'/'relist'/'merger'/'spinoff_parent') so existing consumers (universe line calc, loader) keep working.
import re

LINEAGE_V2_COLS = LINEAGE_COLS + ["ratioSource", "linkClass", "reviewFlag", "sources", "version", "chainGapTradingDays", "chainable"]
LINEAGE_V2_VERSION = "v2-rc2-R3"
SPAC_NAME_RE = re.compile(r"\bACQUISITION\b|\bACQN\b|\bACQ\b|\bSPAC\b|BLANK CHECK|MERGER CORP|\bOPPORTUNIT|GROWTH CORP|INVESTMENT CORP|"
                          r"CAPITAL (?:CORP|CO|I|II|III|IV|V|ONE|TWO|THREE|FOUR)\b|-\s?REDH|\bREDH\b|^TWO$|PRINCIPAL \d+ MERGER", re.I)
TRANSITION_TOL_DAYS = 12          # co_acthist datadate window around the transition for a text ratio
_NUM = r"(\d{1,3}(?:,\d{3})+|\d*\.?\d+)"


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def parse_exchange_ratio(text) -> tuple[float, str]:
    """(new shares per old share, kind) parsed from a co_acthist action text; (NaN, '') when nothing is stated.
    kind: 'reverse_split' (1-FOR-N REVERSE), 'split' (X-FOR-1 STK SPLIT), 'exchange' (N SHARES OF .. FOR EVERY SHARE),
    'reverse_split_fractional' (1-FOR-x.xx REVERSE with a non-integer x: Compustat writes exchange ratios this way too,
    so the direction is ambiguous and must be resolved by the caller)."""
    if not isinstance(text, str) or not text.strip():
        return np.nan, ""
    t = text.upper()
    m = re.search(r"\b1[- ]FOR[- ]" + _NUM + r"\s*(?:REVERSE|REV\b)", t) or re.search(r"\bONE[- ]FOR[- ]" + _NUM + r"\s*REVERSE", t)
    if m:
        n = _num(m.group(1))
        if n <= 0:
            return np.nan, ""
        return 1.0 / n, ("reverse_split" if abs(n - round(n)) < 1e-9 else "reverse_split_fractional")
    m = re.search(_NUM + r"[- ]FOR[- ]" + _NUM + r"\s*REVERSE", t)          # "5-FOR-32 REVERSE", ".7194-FOR-1 REVERSE"
    if m:
        a, b = _num(m.group(1)), _num(m.group(2))
        if a > 0 and b > 0:
            return a / b, "reverse_split"
    m = re.search(r"REVERSE (?:STOCK )?SPLIT[^0-9]{0,40}\b1[- ]FOR[- ]" + _NUM, t)
    if m:
        return 1.0 / _num(m.group(1)), "reverse_split"
    m = re.search(_NUM + r"[- ]FOR[- ]" + _NUM + r"\s*(?:STOCK |STK )?SPLIT", t)
    if m:
        a, b = _num(m.group(1)), _num(m.group(2))
        if a > 0 and b > 0:
            return a / b, "split"
    m = re.search(_NUM + r"\s*(?:SHARES?|SHS?)\s+OF\s+[A-Z0-9&.' -]{2,40}?\s+FOR\s+(?:EVERY|EACH)\s+(?:ONE\s+)?(?:SHARE|SH)\b", t)
    if m:
        return _num(m.group(1)), "exchange"
    m = re.search(r"(?:EXCHANGED FOR|CONVERTED INTO|RECEIVED?)\s+" + _NUM + r"\s+(?:NEW\s+)?(?:SHARES?|SHS?|COMMON)", t)
    if m:
        return _num(m.group(1)), "exchange"
    if re.search(r"CONVERTED INTO ONE SHARE|1-FOR-1 BASIS|ONE[- ]FOR[- ]ONE", t):
        return 1.0, "exchange"
    return np.nan, ""


# public deal terms for the large names whose ratio is stated nowhere in xfeed (co_acthist N/C rows carry no terms).
# They are estimates by construction ([추정], isEstimate for R1); the price-continuity check is reported next to them.
MANUAL_TERMS = {
    (50158704, 2641643): dict(ratio=1.72932, linkClass="merger", note="US WEST -> Qwest 2000-06-30: 1.72932 Q per USW (public deal terms) [추정]"),
    (2588692, 24064340): dict(ratio=0.4125, linkClass="merger", note="America West Holdings -> US Airways Group 2005-09-27: 0.4125 LCC per AWA class B (public deal terms) [추정]"),
    (2639834, 548241053): dict(ratio=0.4, linkClass="merger", note="PotashCorp -> Nutrien 2018-01-02: 0.4 NTR per POT (public deal terms) [추정]"),
    (67061158, 416056350): dict(ratio=1.4663, linkClass="merger", note="Colony Capital -> Colony NorthStar 2017-01-10: 1.4663 CLNS per CLNY class A (public deal terms) [추정]"),
    (2644060, 642468719): dict(ratio=0.8042, linkClass="merger", note="Rudolph Technologies -> Onto Innovation 2019-10-28: 0.8042 ONTO per RTEC (public deal terms; Nanometrics 1.0) [추정]"),
    (102338833, 9404997): dict(ratio=0.3188, linkClass="merger", note="Click2learn -> SumTotal Systems 2004-03-19: 0.3188 SUMT per CLKS (public deal terms; Docent 0.7327) [추정]"),
    (22796851, 115572134): dict(ratio=0.77, linkClass="merger", note="Inergy Holdings -> Inergy LP 2010-11-08: 0.77 NRGY per NRGP unit (public deal terms) [추정]"),
    (2626596, 27059655): dict(ratio=0.05, linkClass="spin_off", note="Liberty Media -> Liberty Capital 2006-05-10: 0.05 LCAP.A per LMG.A + 0.25 LINT.A (27059653) per LMG.A (public terms) [추정]",
                              child=27059653, childRatio=0.25),
    (2626898, 412297487): dict(ratio=0.5, linkClass="spin_off", note="Lions Gate reclassification 2016-12-09: 0.5 LGF.A + 0.5 LGF.B (412297486) per old LGF (co_acthist 1-FOR-2; class B leg public terms) [추정]",
                               child=412297486, childRatio=0.5),
    (2586651, 4027348): dict(ratio=1.0, linkClass="spin_off", note="Acacia Research 2002-12-16 tracking stocks: 1 ACTG + 1 CBMX (4027347) per ACRI (public terms) [추정]",
                             child=4027347, childRatio=1.0),
    (695981898, 10437105): dict(ratio=1.0, linkClass="spin_off", note="Viad/MoneyGram reverse spin 2004-07-01: 1 MGI per old VVI + 0.25 new Viad (2662349, 1-for-4 at the spin) per old VVI (co_acthist SPN; ratio public terms) [추정]",
                               child=2662349, childRatio=0.25),
    (83102616, 2613020): dict(ratio=0.2, linkClass="merger", cash=12.5,
                              note="Flowers Industries -> Kellogg 2001-03-27: 0.2 Flowers Foods per share = co_acthist 'ONE SH OF FLOWERS FOODS ... FOR EVERY FIVE SHARES'; cash 'APPROX US$12.50/SH' from the same text [추정 amount]; price continuity +0.7%"),
    (2623775, 83037516): dict(ratio=3.8031, linkClass="merger", note="Kennedy-Wilson Inc -> Kennedy-Wilson Holdings (Prospect Acquisition) 2009-11-17: 3.8031 KW per KWIC [추정 direction] (co_acthist '1-FOR-3.8031' fractional; 3.8031 gives R +0.9%, 1/3.8031 -93%)"),
    (27059654, 326004576): dict(ratio=1.0, linkClass="simple_rename", confidence="low",
                                note="Liberty Interactive series B -> QVC Group series B: class B renamed 1:1 alongside LINT.A -> QRTE.A (co_acthist 2014-10-10 N/C); "
                                     "boundary ambiguous (mi stops the old id 2011-11-10, ciq appends copies of QRTE.B prints to 2018-04-30, successor trades from 2014-10-16) -> not chainable [추정]"),
    (214487986, 116651372): dict(ratio=0.2, linkClass="merger", cash=2.5,
                                 note="Tronox Inc -> Tronox Ltd 2012-06-15: public terms 1 TROX class A + $12.50 per Tronox Inc share [추정]; ciq restated the old id's series on the post-5:1-split basis (priceclose 32.4 = 162/5, adjf 1.0; ciqsplit 116651372@2012-07-26 rate 5) so per old raw share = 0.2 TROX + $2.50 (cache-unit note); price continuity -2.5%"),
    (2636827, 2944882): dict(ratio=1.0 / 3.0, linkClass="merger", note="PMR Corp -> Psychiatric Solutions 2002-08-06: 1-for-3 reverse split at the reverse merger (public terms; price step 2.24 -> 6.75 consistent) [추정]"),
    (2615461, 2666010): dict(ratio=0.665, linkClass="merger", note="Global Marine -> GlobalSantaFe 2001-11-21: 0.665 GSF per GLM (public deal terms) [추정]"),
    (2603892, 27877176): dict(ratio=0.3125, linkClass="merger", note="VantageSouth Bancshares -> Yadkin Financial 2014-07-07: 0.3125 YDKN per VSB (public deal terms) [추정]"),
    (6699559, 49334329): dict(ratio=1.0, linkClass="merger", note="Triarc class B -> Wendy's/Arby's Group class A 2008-09-30: 1 WEN per TRY.B (public deal terms; the new id's pre-2008 history is the Triarc class A line, not a copy of class B) [추정]"),
}
# transitions whose old holders also received cash / a second security: handed to R1 (for_R1_events.csv)
MANUAL_LEGS = {
    (28261956, 283620484): dict(cash=9.12, child=115907529, childRatio=0.1809, note="Atlas Energy LP 2015-02-27: $9.12 cash + 0.1809 TRGP (115907529) per ATLS unit (public deal terms) [추정]; 0.5 AEG per unit = co_acthist 1-FOR-2"),
    (22260348, 290977196): dict(child=59434239, childRatio=0.2016, note="Paramount Gold and Silver 2015-04-20: 0.2016 CDE (59434239) per PZG (public deal terms) [추정]; 1/20 Paramount Gold Nevada = co_acthist 1-FOR-20"),
    (2597514, 2597606): dict(unresolved=True, note="Canadian Pacific Ltd 2001-10-03 five-way split: CPR (2597606) + Fording (2613223) + CP Ships (2603772) + Fairmont (2610991) + PanCanadian (2609168); per-share ratios not in xfeed -> unresolved"),
    (2644409, 262300822): dict(child=263819154, childRatio=1.0, note="SLM -> Navient 2014-05-01 reverse spin-off: co_acthist SPN 'RECEIVED 1 SHARE OF NEWLY CREATED ENTITY SLM CORP' -> child 263819154 (new SLM, close 9.02 on 2014-05-01); the proto distributions row pointing at 1979397334 (no price) is the same leg mis-resolved -> ignore"),
    # Discovery Holding 2008-09-18: 1-for-2 reverse split into Series A (49031559) + 0.5 Series C (49031561 DISC.K, close 16.00) + 0.05 Ascent Media A (ledger ok row)
    (22967213, 49031559): dict(child=49031561, childRatio=0.5, note="Discovery Holding -> Discovery Communications 2008-09-18: co_acthist '1-FOR-2 REVERSE STOCK SPLIT ... 0.5 CLASS C SHARE ALSO RECEIVED' -> 0.5 DISC.K (49031561) per old share; 0.05 ASCM.A is the distributions/ledger row"),
}
CASH_TYPES = (1, 4, 5, 6, 7, 8, 10, 15, 17, 18, 19, 23, 33)     # same cash dividend types as ledger_build.CASH_TYPES
BOUNDARY_TAIL_MIN_DAYS = 20        # F5: a lifecycle cap this far before the events endDate is re-examined against the old id's own closes


def _pull(db: DB, work: Path, name: str, sql: str, keys, chunk_size: int = 1000) -> pd.DataFrame:
    """Chunked pull cached as work/<name>.parquet (delete the file to redo)."""
    out = work / f"{name}.parquet"
    if out.exists():
        return pd.read_parquet(out)
    parts = []
    keys = sorted(set(keys))
    for ch in (list(keys[i:i + chunk_size]) for i in range(0, len(keys), chunk_size)):
        parts.append(db.query(sql, (ch,)))
    df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    work.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, **mb.PARQUET_KW)
    return df


def pull_rename_evidence(db: DB, rej: pd.DataFrame, work: Path, lifecycle: pd.DataFrame | None = None) -> dict:
    """DB evidence for the rejected rows (ids from both sides; co_acthist / sec_idhist by the shared gvkeys).  The price window
    starts 30 days before min(events endDate, lifecycle lastTradeDate of the old id) and ends 45 days after the endDate."""
    ids = sorted(set(rej.tid.astype(int)) | set(rej.succ.astype(int)))
    ltd = lifecycle.set_index("tradingItemId").lastTradeDate if lifecycle is not None else None
    ev = {}
    ev["ident"] = _pull(db, work, "rename_identity", f"""
        select ti.tradingitemid, ti.securityid, ti.tickersymbol, ti.exchangeid, ti.currencyid, ti.tradingitemstatusid,
               s.companyid, s.securityname, s.securitystartdate, s.securityenddate, s.securitysubtypeid, c.companyname
        from {SCHEMA}.ciqtradingitem ti join {SCHEMA}.ciqsecurity s on s.securityid = ti.securityid
        left join {SCHEMA}.ciqcompany c on c.companyid = s.companyid where ti.tradingitemid = any(%s)""", ids)
    ev["gmap"] = _pull(db, work, "rename_gvkeymap", f"select objectid, gvkey, iid, activeflag, symbolstartdate, symbolenddate "
                                                    f"from {SCHEMA}.ciqgvkeyiid where objectid = any(%s)", ids)
    gvkeys = sorted(set(ev["gmap"].gvkey.dropna()))
    ev["act"] = _pull(db, work, "rename_coacthist", f"select * from {SCHEMA}.co_acthist where gvkey = any(%s)", gvkeys)
    ev["split"] = _pull(db, work, "rename_ciqsplit", f"select * from {SCHEMA}.ciqsplit where tradingitemid = any(%s)", ids)
    ev["div"] = _pull(db, work, "rename_dividends", f"select tradingitemid, exdate, paydate, divamount, dividendtypeid, currencyid "
                                                    f"from {SCHEMA}.ciqdividend where tradingitemid = any(%s)", ids)
    # raw closes around each transition (two-id index scans, [end-30d, end+45d])
    out = work / "rename_prices.parquet"
    if out.exists():
        ev["px"] = pd.read_parquet(out)
    else:
        parts = []
        for r in rej.itertuples():
            end = pd.Timestamp(r.end)
            lo = end
            if ltd is not None and pd.notna(ltd.get(int(r.tid), pd.NaT)):
                lo = min(lo, pd.Timestamp(ltd.get(int(r.tid))))
            parts.append(db.query(f"""select tradingitemid, pricingdate, priceclose * adjustmentfactor as raw_close, adjustmentfactor,
                                             volume / adjustmentfactor as raw_volume
                                      from {SCHEMA}.ciqpriceequity where tradingitemid = any(%s) and pricingdate between %s and %s
                                        and priceclose is not null order by 1, 2""",
                                  ([int(r.tid), int(r.succ)], (lo - pd.Timedelta(days=30)).date(), (end + pd.Timedelta(days=45)).date())))
        ev["px"] = pd.concat(parts, ignore_index=True).drop_duplicates(["tradingitemid", "pricingdate"])
        ev["px"].to_parquet(out, **mb.PARQUET_KW)
    # raw closes of every child leg (manual legs + proto distributions stock legs) around the transition
    child_ids = sorted({int(v["child"]) for v in list(MANUAL_TERMS.values()) + list(MANUAL_LEGS.values()) if v.get("child")})
    dist_path = mb.SCRATCH / "stage_rc2" / "R3" / "in" / "distributions.parquet"
    if dist_path.exists():
        d = pd.read_parquet(dist_path, columns=["tradingItemId", "exDate", "kind", "status", "newTradingItemId", "ratioNewPerOld"])
        d = d[(d.kind == "stock") & d.newTradingItemId.notna() & d.tradingItemId.isin(set(rej.tid.astype(int)))]
        child_ids = sorted(set(child_ids) | set(int(i) for i in d.newTradingItemId))
        ev["dist"] = d
    else:
        ev["dist"] = pd.DataFrame(columns=["tradingItemId", "exDate", "kind", "status", "newTradingItemId", "ratioNewPerOld"])
    ev["childpx"] = _pull(db, work, "rename_child_prices", f"""select tradingitemid, pricingdate, priceclose * adjustmentfactor as raw_close
                                                            from {SCHEMA}.ciqpriceequity where tradingitemid = any(%s) and pricingdate >= '2000-01-01'
                                                              and priceclose is not null""", child_ids) if child_ids else pd.DataFrame(columns=["tradingitemid", "pricingdate", "raw_close"])
    ev["childpx"]["pricingdate"] = pd.to_datetime(ev["childpx"].pricingdate)
    ev["dist"]["exDate"] = pd.to_datetime(ev["dist"].exDate)
    for k in ("act",):
        ev[k]["datadate"] = pd.to_datetime(ev[k].datadate)
    ev["split"]["exdate"] = pd.to_datetime(ev["split"].exdate)
    ev["div"]["exdate"] = pd.to_datetime(ev["div"].exdate)
    ev["px"]["pricingdate"] = pd.to_datetime(ev["px"].pricingdate)
    return ev


def collect_legs(tid: int, new: int, ratio: float, old_last_date, new_first_date, old_last_close: float, new_first_close: float,
                 dv: pd.DataFrame, dist: pd.DataFrame | None, childpx: pd.DataFrame | None, manual: dict, mlegs: dict) -> dict:
    """F1: every leg of the consideration for one old share at the transition (old_last_date -> new_first_date):
      cash   ciqdividend cash-type rows of the OLD id with exDate in (old_last, new_first + 3d]  (+ manual cash)
             unit: the vendor states the amount on the ex-date share basis of the id it is attached to, which for a reverse-split
             rename can be the NEW basis (LQ 16.80 per CPLG = 8.40 per LQ) or the OLD basis (TALS 1.5118 per Talaris); the basis is
             chosen by total-consideration consistency (smaller |R_total|) and recorded in the evidence
      stock  successor leg (ratio) + child legs: manual (public terms) + proto distributions stock rows of the old id in the gap
             (status ok/value_mismatch/no_price), valued at the child's raw close on new_first_date (or the first close within 5 days)
    Returns cash per old share, stock leg list, R_total, partial flag (any leg unvalued / unknown ratio)."""
    lo = old_last_date if pd.notna(old_last_date) else pd.NaT
    hi = (new_first_date + pd.Timedelta(days=3)) if pd.notna(new_first_date) else pd.NaT
    out = {"cash": 0.0, "cashSource": "", "stock": [], "stockText": "", "R_total": np.nan, "partial": False, "partialNote": "", "flag": ""}
    if pd.isna(lo) or pd.isna(hi):
        return out
    succ_value = ratio * new_first_close if np.isfinite(ratio) and np.isfinite(new_first_close) else np.nan
    cash_rows = dv[(dv.tradingitemid == tid) & dv.dividendtypeid.isin(CASH_TYPES) & dv.divamount.notna() & (dv.exdate > lo) & (dv.exdate <= hi)]
    cash_items, cash_src = [], []
    for d in cash_rows.itertuples():
        amt = float(d.divamount)
        cands = {"as_recorded": amt}
        if np.isfinite(ratio) and abs(ratio - 1) > 1e-9:
            cands["new_basis_x_ratio"] = amt * ratio
        best_key, best_r = "as_recorded", np.nan
        if np.isfinite(succ_value) and np.isfinite(old_last_close) and old_last_close > 0:
            rs = {k: abs((succ_value + v) / old_last_close - 1) for k, v in cands.items()}
            best_key = min(rs, key=rs.get)
            best_r = rs[best_key]
        cash_items.append(cands[best_key])
        cash_src.append(f"ciqdividend {d.exdate.date()} type{int(d.dividendtypeid)} {amt:.6g}" + (f" ({best_key}: {cands[best_key]:.6g}/old share)" if best_key != "as_recorded" else " (per old share)"))
    if manual.get("cash") or mlegs.get("cash"):
        c = float(manual.get("cash", mlegs.get("cash")))
        if not any(abs(c - x) / max(c, 1e-9) < 0.05 for x in cash_items):
            cash_items.append(c)
            cash_src.append(f"manual {c:.6g} [추정]")
    out["cash"] = float(sum(cash_items))
    out["cashSource"] = "; ".join(cash_src)
    stock = []
    seen = set()
    for src, entry in (("manual", manual), ("manual", mlegs)):
        if entry.get("child") and int(entry["child"]) not in seen:
            stock.append({"child": int(entry["child"]), "ratio": float(entry.get("childRatio", np.nan)), "source": src + "[추정]"})
            seen.add(int(entry["child"]))
    if dist is not None and len(dist):
        dd = dist[(dist.tradingItemId == tid) & (dist.exDate > lo) & (dist.exDate <= hi) & dist.newTradingItemId.notna()]
        for d in dd.itertuples():
            cid = int(d.newTradingItemId)
            if cid in seen or cid == new:
                continue
            if seen and str(d.status) in ("no_price", "ratio_unknown", "new_id_unknown"):
                continue      # a manual leg covers this event; the proto row is the same leg with an unresolved id/ratio
            stock.append({"child": cid, "ratio": float(d.ratioNewPerOld) if pd.notna(d.ratioNewPerOld) else np.nan, "source": f"distributions:{d.status}"})
            seen.add(cid)
    total = succ_value + out["cash"]
    partial, notes = False, []
    if not np.isfinite(succ_value):          # ratio unknown / successor unpriced: the row is unresolved anyway (no events rebuild)
        out["stock"] = stock
        out["stockText"] = "; ".join(f"{l['child']}x{l['ratio']:.6g}[{l['source']}]" for l in stock)
        out["flag"] = ("multi_leg: " + ("cash=%.6g/old share; " % out["cash"] if out["cash"] > 0 else "") + ("stock=" + out["stockText"] if stock else "")).rstrip("; ") if (stock or out["cash"] > 0) else ""
        return out
    for leg in stock:
        px_c = childpx[(childpx.tradingitemid == leg["child"]) & (childpx.pricingdate >= new_first_date) & (childpx.pricingdate <= new_first_date + pd.Timedelta(days=7))] if childpx is not None else None
        close = float(px_c.sort_values("pricingdate").raw_close.iloc[0]) if px_c is not None and len(px_c) else np.nan
        leg["close"] = close
        if np.isfinite(leg["ratio"]) and np.isfinite(close):
            leg["value"] = leg["ratio"] * close
            total += leg["value"]
        else:
            leg["value"] = np.nan
            partial = True
            notes.append(f"child {leg['child']} " + ("ratio unknown" if not np.isfinite(leg["ratio"]) else "no close at transition"))
    out["stock"] = stock
    out["stockText"] = "; ".join(f"{l['child']}x{l['ratio']:.6g}@{l['close']:.6g}[{l['source']}]" for l in stock)
    out["partial"] = partial
    out["partialNote"] = "; ".join(notes)
    if np.isfinite(old_last_close) and old_last_close > 0 and not partial:
        out["R_total"] = total / old_last_close - 1
    parts = []
    if out["cash"] > 0:
        parts.append(f"cash={out['cash']:.6g}/old share ({out['cashSource']})")
    if stock:
        parts.append("stock=" + out["stockText"])
    out["flag"] = ("multi_leg: " + "; ".join(parts)) if parts else ""
    return out


def classify_rename_rejections(rej: pd.DataFrame, ev: dict, lifecycle: pd.DataFrame, t7_class: pd.DataFrame | None = None) -> pd.DataFrame:
    """One row per rejected pair: linkClass, relation, ratio, ratioSource, confidence, evidence, reviewFlag, legs for R1."""
    ident = ev["ident"].set_index("tradingitemid")
    gmap, act, spl, dv, px = ev["gmap"], ev["act"], ev["split"], ev["div"], ev["px"]
    lc = lifecycle.set_index("tradingItemId")
    t7 = t7_class.set_index(["tid", "succ"])["class"] if t7_class is not None else None
    rows = []
    for r in rej.itertuples():
        tid, new, end = int(r.tid), int(r.succ), pd.Timestamp(r.end)
        # the transition is measured at the old line's last genuine close (lifecycle), never later: an events endDate after
        # lastTradeDate (T6 enddate_gap rows) would put the boundary inside the successor's stitched tail
        lt = lc.lastTradeDate.get(tid, pd.NaT)
        end_capped = pd.notna(lt) and pd.Timestamp(lt) < end
        po = px[(px.tradingitemid == tid)].sort_values("pricingdate")
        pn = px[(px.tradingitemid == new)].sort_values("pricingdate")
        boundary_note = ""
        if end_capped:
            # F5: keep the cap when the old id's closes after lastTradeDate are a copy of the successor (stitched tail); when they are
            # the old line's own prints (successor absent / prices differ) the mi-based cut is a vendor boundary disagreement and the
            # transition is measured at the ciq boundary (events endDate)
            tail = po[(po.pricingdate > pd.Timestamp(lt)) & (po.pricingdate <= end)]
            cap_days = (end - pd.Timestamp(lt)).days
            new_first_trade = lc.firstTradeDate.get(new, pd.NaT)
            if len(tail) and cap_days > BOUNDARY_TAIL_MIN_DAYS:
                tail_new = pn.set_index("pricingdate").raw_close.reindex(tail.pricingdate)
                eq = np.abs(tail.raw_close.to_numpy(dtype=float) / tail_new.to_numpy(dtype=float) - 1) < 0.003
                copy_share = float(np.nan_to_num(eq, nan=False).mean())
                succ_trading = pd.notna(new_first_trade) and pd.Timestamp(new_first_trade) <= end      # successor already live at the mi cut
                if copy_share < 0.5 or not succ_trading:
                    boundary_note = (f"vendor_boundary_disagreement(mi lastTradeDate {pd.Timestamp(lt).date()} vs ciq own prints to {end.date()}: "
                                     f"{len(tail)} closes, {copy_share:.0%} successor copies, successor firstTradeDate "
                                     f"{'' if pd.isna(new_first_trade) else pd.Timestamp(new_first_trade).date()}) -> measured at the ciq boundary; not chainable from the lifecycle window")
                    end_capped = False
        if end_capped:
            end = pd.Timestamp(lt)
        io, inn = ident.loc[tid], ident.loc[new]
        shared = sorted(set(zip(gmap[gmap.objectid == tid].gvkey, gmap[gmap.objectid == tid].iid))
                        & set(zip(gmap[gmap.objectid == new].gvkey, gmap[gmap.objectid == new].iid)))
        gv = shared[0] if shared else (None, None)
        old_last = po[po.pricingdate <= end]
        old_last_close = float(old_last.raw_close.iloc[-1]) if len(old_last) else np.nan
        old_last_date = old_last.pricingdate.iloc[-1] if len(old_last) else pd.NaT
        new_after = pn[pn.pricingdate > end]
        new_first_close = float(new_after.raw_close.iloc[0]) if len(new_after) else np.nan
        new_first_date = new_after.pricingdate.iloc[0] if len(new_after) else pd.NaT
        common = sorted(set(po.pricingdate) & set(pn.pricingdate))
        n_common, n_eq, const_ratio = len(common), 0, False
        if n_common:
            a = po.set_index("pricingdate").raw_close.reindex(common).to_numpy(dtype=float)
            b = pn.set_index("pricingdate").raw_close.reindex(common).to_numpy(dtype=float)
            n_eq = int((np.abs(a / b - 1) < 0.003).sum())
            ok = (a > 0) & (b > 0)
            if ok.sum() >= 3:      # a copy restated by a later factor (5% stock dividend, split) has a constant price ratio
                lr = np.log(a[ok] / b[ok])
                const_ratio = bool(np.nanstd(lr) < 0.003 and abs(np.nanmean(lr)) > 0.003)
        stitched = n_common > 0 and (n_eq / n_common >= 0.9 or const_ratio)
        coexist = n_common >= 5 and n_eq / n_common < 0.5 and not const_ratio   # two lines quoted at different prices on the same days
        same_company = bool(io.companyid == inn.companyid)
        # co_acthist rows of the shared gvkey around the transition
        a = act[act.gvkey == gv[0]] if gv[0] else act.iloc[0:0]
        near = a[(a.datadate - end).abs() <= pd.Timedelta(days=200)]
        nc = near[near.actioncd == "N/C"].copy()
        nc["_d"] = (nc.datadate - end).abs()
        nc = nc.sort_values("_d")
        spn = near[(near.actioncd == "SPN") & ((near.datadate - end).abs() <= pd.Timedelta(days=20))]
        nc_txt = "; ".join(f"{d.date()} N/C {(t or '').strip()} [{oc}->{nn}]" for d, t, oc, nn in zip(nc.datadate, nc.actiontxt, nc.oldconm, nc.newconm))[:500]
        old_names = " ".join(str(x) for x in list(nc.oldconm.dropna()) + [io.companyname or ""])
        spac_old = (end.year >= 2005) and (bool(SPAC_NAME_RE.search(old_names)) or bool(re.search(r"REDH", str(io.securityname), re.I)))
        spac_new = bool(SPAC_NAME_RE.search(str(inn.companyname))) and not bool(re.search(r"REDH", str(nc.newconm.iloc[0]) if len(nc) else "", re.I))
        # --- ratio sources ------------------------------------------------------------------------------
        sources, ratio, ratio_src, kind = [], np.nan, "", ""
        s_win = spl[spl.tradingitemid.isin([tid, new]) & spl.exdate.notna()]
        if pd.notna(old_last_date) and pd.notna(new_first_date):
            s_win = s_win[(s_win.exdate > old_last_date) & (s_win.exdate <= new_first_date + pd.Timedelta(days=5))]
            # a row dated a few days after the new id's first close is still the transition split unless the vendor applied
            # it inside the new series (adjustmentfactor step of that size at the exdate)
            keep = []
            for sr in s_win.itertuples():
                if sr.exdate <= new_first_date:
                    keep.append(True)
                    continue
                before = pn[(pn.pricingdate < sr.exdate) & (pn.pricingdate >= new_first_date)]
                after = pn[pn.pricingdate >= sr.exdate]
                if len(before) and len(after):
                    k = float(after.adjustmentfactor.iloc[0]) / float(before.adjustmentfactor.iloc[-1])
                    rate = float(sr.rate)
                    keep.append(not (np.isclose(k, rate, rtol=0.02) or np.isclose(k, 1.0 / rate, rtol=0.02)))
                else:
                    keep.append(False)
            s_win = s_win[np.asarray(keep, dtype=bool)] if len(s_win) else s_win
        else:
            s_win = s_win.iloc[0:0]
        if len(s_win):
            rates = s_win.drop_duplicates("exdate").rate.astype(float)
            ratio, ratio_src = float(np.prod(rates)), "ciqsplit"
            sources.append("ciqsplit:" + ";".join(f"{i}@{d.date()} rate={float(x):.6g} type={t}" for i, d, x, t in zip(s_win.tradingitemid, s_win.exdate, s_win.rate, s_win.splittypeid)))
        text_ratio, text_kind, text_date = np.nan, "", pd.NaT
        for d, t in zip(nc.datadate, nc.actiontxt):
            tr, tk = parse_exchange_ratio(t)
            lo = (old_last_date if pd.notna(old_last_date) else end) - pd.Timedelta(days=TRANSITION_TOL_DAYS)
            hi = (new_first_date if pd.notna(new_first_date) else end) + pd.Timedelta(days=TRANSITION_TOL_DAYS)
            if np.isfinite(tr) and lo <= d <= hi:
                text_ratio, text_kind, text_date = tr, tk, d
                break
        # a split the vendor already carries inside one of the two series (ciqsplit row outside the gap, or an adjustmentfactor
        # step on the old id's last day / right after the new id's first day) is not a transition ratio -> 1.0 at the transition
        inside_series = ""
        if np.isfinite(text_ratio) and not len(s_win):
            s_any = spl[spl.tradingitemid.isin([tid, new]) & spl.exdate.notna() & ((spl.exdate - end).abs() <= pd.Timedelta(days=45))]
            hit = s_any[np.isclose(s_any.rate.astype(float), text_ratio, rtol=0.02) | np.isclose(s_any.rate.astype(float), 1.0 / text_ratio, rtol=0.02)]
            if len(hit):
                inside_series = "ciqsplit " + "; ".join(f"{i}@{d.date()} rate={float(x):.6g}" for i, d, x in zip(hit.tradingitemid, hit.exdate, hit.rate)) + " outside the gap"
            if not inside_series and len(old_last) >= 2:
                k_old = float(old_last.adjustmentfactor.iloc[-1]) / float(old_last.adjustmentfactor.iloc[-2])
                if abs(k_old - 1) > 1e-6 and (np.isclose(k_old, text_ratio, rtol=0.02) or np.isclose(k_old, 1.0 / text_ratio, rtol=0.02)):
                    inside_series = f"old id adjustmentfactor step {k_old:.6g} on {old_last_date.date()}"
            if not inside_series and len(new_after) >= 2:
                k_new = float(new_after.adjustmentfactor.iloc[-1]) / float(new_after.adjustmentfactor.iloc[0])
                if abs(k_new - 1) > 1e-6 and (np.isclose(k_new, text_ratio, rtol=0.02) or np.isclose(k_new, 1.0 / text_ratio, rtol=0.02)):
                    inside_series = f"new id adjustmentfactor step {k_new:.6g} after {new_first_date.date()}"
        if np.isfinite(text_ratio) and inside_series:
            sources.append(f"co_acthist:{text_date.date()} {text_kind} {text_ratio:.6g} is carried inside a series ({inside_series}) -> transition ratio 1")
            text_ratio, text_kind = 1.0, "split_inside_series"
        if np.isfinite(text_ratio):
            if text_kind == "reverse_split_fractional" and np.isfinite(old_last_close) and np.isfinite(new_first_close):
                r1 = new_first_close * text_ratio / old_last_close - 1
                r2 = new_first_close / text_ratio / old_last_close - 1
                if abs(r2) < abs(r1):          # Compustat wrote an exchange ratio as '1-FOR-x.xx REVERSE'
                    text_ratio, text_kind = 1.0 / text_ratio, "exchange(direction by price step)"
            sources.append(f"co_acthist:{text_date.date()} {text_kind} -> {text_ratio:.6g}")
            if not np.isfinite(ratio):
                ratio, ratio_src = float(text_ratio), "co_acthist_text"
            elif abs(text_ratio / ratio - 1) > 0.02:
                sources.append(f"conflict:text {text_ratio:.6g} vs ciqsplit {ratio:.6g} (ciqsplit kept)")
        manual = MANUAL_TERMS.get((tid, new))
        mt = manual or {}
        if manual is not None:
            if np.isfinite(ratio) and abs(manual["ratio"] / ratio - 1) > 0.02:
                sources.append(f"manual {manual['ratio']:.6g} differs from {ratio_src} {ratio:.6g}: manual kept")
            ratio, ratio_src = float(manual["ratio"]), "manual_public_terms"
            sources.append("manual:" + manual["note"])
        # --- class --------------------------------------------------------------------------------------
        mlegs = MANUAL_LEGS.get((tid, new), {})
        merger_text = bool(re.search(r"MERGED|MERGER|ACQUIRED|ACQUIRING|EXCHANGED FOR|EXCHANGING EACH|ACCOUNTING SURVIVOR|SHARES OF .{2,40} FOR (?:EVERY|EACH)", nc_txt, re.I))
        has_spn = len(spn) > 0 or bool(dv[(dv.tradingitemid == tid) & (dv.dividendtypeid == 20) & ((dv.exdate - end).abs() <= pd.Timedelta(days=12))].shape[0]) \
            or bool(re.search(r"SPIN[- ]?OFF|SPUN[- ]?OFF|TRACKING STOCK", nc_txt, re.I))
        r_step = new_first_close / old_last_close - 1 if np.isfinite(old_last_close) and np.isfinite(new_first_close) and old_last_close > 0 else float(r.R)
        differing_common = n_common >= 3 and n_eq / n_common < 0.5
        no_link = len(nc) == 0 and not stitched
        exch_changed = pd.notna(io.exchangeid) and pd.notna(inn.exchangeid) and int(io.exchangeid) != int(inn.exchangeid)
        if manual is not None and manual.get("linkClass"):
            cls = manual["linkClass"]
        elif mlegs and "child" in mlegs and "cash" in mlegs:
            cls = "merger"
        elif (tid, new) in MANUAL_LEGS:
            cls = "spin_off"
        elif no_link and (coexist or differing_common or not np.isfinite(new_first_close) or abs(r_step) > 5):
            cls = "mismatch"
        elif has_spn:
            cls = "spin_off"
        elif spac_old and not spac_new:
            cls = "de_spac"
        elif merger_text or coexist:      # two lines quoted at different prices on the same days cannot be one security
            cls = "merger"
        elif not same_company and re.search(r"EMERGE|BANKRUPTCY|REORGANI", nc_txt, re.I):
            cls = "relist"
        elif exch_changed:
            cls = "relist"
        else:
            cls = "simple_rename"
        relation = {"simple_rename": "rename", "relist": "relist", "de_spac": "rename", "merger": "merger", "spin_off": "spinoff_parent",
                    "mismatch": "unknown"}[cls]
        if cls == "spin_off":
            relation = "rename"           # the parent line continues in the new id; the child leg goes to R1
        # --- ratio fallback and confidence -------------------------------------------------------------------
        r_adopted = new_first_close * ratio / old_last_close - 1 if np.isfinite(ratio) and np.isfinite(old_last_close) and np.isfinite(new_first_close) and old_last_close > 0 else np.nan
        price_implied = old_last_close / new_first_close if np.isfinite(old_last_close) and np.isfinite(new_first_close) and new_first_close > 0 else np.nan
        review = []
        if cls == "mismatch":
            ratio, ratio_src, conf = np.nan, "none", "low"
            review.append("mismatch: no N/C row and no stitched copy; two distinct lines share the Compustat gvkey/iid")
        elif not np.isfinite(ratio):
            structural_1to1 = stitched or same_company or cls in ("de_spac", "spin_off")
            if cls == "merger":
                ratio, ratio_src, conf = np.nan, "unresolved", "low"
                review.append(f"ratio_unresolved(merger terms not in xfeed; price_implied={price_implied:.4g}; 1:1 gives R {r_step:+.1%})")
            elif cls == "spin_off" and not mlegs.get("unresolved"):
                ratio, ratio_src, conf = 1.0, "assumed_1to1", "medium"
                review.append(f"parent line continues 1:1; the step {r_step:+.1%} is the child leg (R1 ledger / distributions)")
            elif cls == "spin_off":
                ratio, ratio_src, conf = np.nan, "unresolved", "low"
                review.append(f"ratio_unresolved(multi-way split terms not in xfeed; price_implied={price_implied:.4g})")
            elif cls == "de_spac":
                ratio, ratio_src, conf = 1.0, "assumed_1to1", "medium"
                if abs(r_step) > 0.2:
                    review.append(f"de-SPAC first-day move {r_step:+.1%} kept as real (P12)")
            # F6 rule: the LINK is decided by identifiers (shared gvkey/iid + N/C row); only the RATIO needs a dated source.  With no
            # dated source the 1:1 default (Compustat N/C = name change) is taken when price does not contradict it (|step| <= 50%,
            # supporting evidence only); a contradicted 1:1 leaves the ratio unknown - the link itself is never dropped for price.
            elif abs(r_step) <= 0.5:
                ratio, ratio_src, conf = 1.0, "assumed_1to1", ("medium" if structural_1to1 else "low")
                if not structural_1to1:
                    review.append(f"assumed 1:1 without terms (1:1 gives R {r_step:+.1%})")
            else:
                ratio, ratio_src, conf = np.nan, "unresolved", "low"
                why = ("stitched same line, the step is a restatement / second-step conversion without a dated ratio" if stitched
                       else ("same company, no dated exchange terms" if same_company else "no dated exchange terms"))
                review.append(f"ratio_unresolved({why}; link kept by identifier evidence; price_implied={price_implied:.4g} not adopted; 1:1 gives R {r_step:+.1%})")
            r_adopted = new_first_close * ratio / old_last_close - 1 if np.isfinite(ratio) and np.isfinite(old_last_close) and new_first_close > 0 else np.nan
        else:
            conf = "high" if ratio_src == "ciqsplit" or (ratio_src == "co_acthist_text" and text_kind in ("reverse_split", "split", "exchange")) else "medium"
            if ratio_src == "manual_public_terms":
                conf = mt.get("confidence", "medium")
            if np.isfinite(r_adopted) and abs(r_adopted) > 0.5:
                review.append(f"adopted ratio still leaves R {r_adopted:+.1%} (market move / concurrent cash leg; see ciqdividend)")
        if mlegs.get("note"):
            review.append(mlegs["note"])
        if end_capped:
            review.append(f"transition measured at lifecycle lastTradeDate {end.date()} (events endDate {pd.Timestamp(r.end).date()} is later: T6 enddate_gap)")
        if boundary_note:
            review.append(boundary_note)
        # ---- F1: every leg of the consideration, valued at the transition ---------------------------------------------
        legs = collect_legs(tid, new, ratio, old_last_date, new_first_date, old_last_close, new_first_close, dv, ev.get("dist"), ev.get("childpx"),
                            mt, MANUAL_LEGS.get((tid, new), {}))
        if legs["cash"] > 0 or legs["stock"]:
            review.append(legs["flag"])
        if legs["partial"]:
            review.append("partial_terms: " + legs["partialNote"])
        evidence = (f"gvkey/iid {gv[0]}/{gv[1]}; companyId {int(io.companyid)}->{int(inn.companyid)} ({'same' if same_company else 'differs'}); "
                    f"securityId {int(io.securityid)}->{int(inn.securityid)}; {io.securityname}->{inn.securityname}; exch {io.exchangeid}->{inn.exchangeid}; "
                    f"stitched_copy={'Y' if stitched else 'N'}({n_eq}/{n_common} equal closes{', constant ratio' if const_ratio else ''}); old_last {old_last_close:.4g}@{'' if pd.isna(old_last_date) else old_last_date.date()} "
                    f"-> new_first {new_first_close:.4g}@{'' if pd.isna(new_first_date) else new_first_date.date()}; R(1:1) {r.R:+.3f}; "
                    + ("; ".join(sources) + "; " if sources else "") + (nc_txt[:260] if nc_txt else "no N/C row") + ("; SPN:" + "; ".join(str(t)[:120] for t in spn.actiontxt) if len(spn) else ""))
        rows.append(dict(oldId=tid, newId=new, oldSymbol=io.tickersymbol, newSymbol=inn.tickersymbol, endDate=end, linkClass=cls, relation=relation,
                         effectiveDate=new_first_date if pd.notna(new_first_date) else end + pd.Timedelta(days=1), ratio=ratio, ratioSource=ratio_src,
                         confidence=conf, evidence=evidence[:900], reviewFlag=" | ".join(review), R_1to1=r_step, R_adopted=r_adopted, R_t6=float(r.R),
                         priceImplied=price_implied, oldLastClose=old_last_close, oldLastDate=old_last_date, newFirstClose=new_first_close,
                         newFirstDate=new_first_date, sameCompany=same_company, stitchedCopy=stitched, coexist=coexist, spacOld=spac_old,
                         oldCompany=io.companyname, newCompany=inn.companyname, gvkey=gv[0], iid=gv[1], ncRows=len(nc), spnRows=len(spn),
                         cashLeg=legs["cash"] if legs["cash"] > 0 else np.nan, cashLegSource=legs["cashSource"], childId=mt.get("child", MANUAL_LEGS.get((tid, new), {}).get("child")),
                         childRatio=float(mt.get("childRatio", MANUAL_LEGS.get((tid, new), {}).get("childRatio", np.nan))),
                         stockLegs=legs["stockText"], R_total=legs["R_total"], legsComplete=not legs["partial"], legsFlag=legs["flag"],
                         boundaryNote=boundary_note,
                         t7Class=t7.get((tid, new)) if t7 is not None else None,
                         oldLastTradeDate=lc.lastTradeDate.get(tid), newFirstTradeDate=lc.firstTradeDate.get(new)))
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------------------
# lineage v2 assembly
# --------------------------------------------------------------------------------------
# same-security links confirmed for the rc1 first-day-NaN ids (T7 item I) and the TEVA ADR -> ordinary transition;
# ratio verified against raw closes on both sides of the transition (R3 report section 5/6).
LINEAGE_V2_OVERRIDES = [
    dict(oldId=2640740, newId=133997359, relation="rename", ratio=0.4464, confidence="high", ratioSource="ciqsplit",
         evidence="ProLogis -> Prologis 2011-06-03: 0.4464 new PLD per old PLD (ciqsplit 2640740@2011-06-03 rate 0.4464; co_acthist '0.4464-FOR-1'); raw 15.21 -> 34.00x0.4464 = 15.18 (-0.2%)"),
    dict(oldId=2586170, newId=133997359, relation="merger", ratio=1.0, confidence="high", ratioSource="events",
         evidence="AMB Property -> Prologis 2011-06-03: AMB legal survivor renamed, 1 new PLD per AMB (events ACQ); raw 34.07 -> 34.00 (-0.2%)"),
    dict(oldId=27059653, newId=204606934, relation="rename", ratio=1.0, confidence="high", ratioSource="stitched_1to1",
         evidence="Liberty Interactive split-off 2011-09-26: LINTA continues 1:1 (gvkeyiid + events N/C); raw 15.98 -> 15.97"),
    dict(oldId=27059655, newId=165754337, relation="rename", ratio=1.0, confidence="high", ratioSource="stitched_1to1",
         evidence="Liberty Capital -> Starz 2011-11-29: LCAPA renamed STRZA 1:1 (gvkeyiid + events N/C); raw 75.85 -> 75.68"),
    dict(oldId=204606934, newId=326004249, relation="rename", ratio=1.0, confidence="high", ratioSource="stitched_1to1",
         evidence="Liberty Interactive -> QVC Group 2014-10-15: LINTA renamed QVCA 1:1 (gvkeyiid); the -14.6% step is the 0.14217 Liberty Ventures A (215503912) distribution ex 2014-10-15 (ciqdividend type 20 on 326004249, pay 2014-10-20) -> R1 leg [추정 ratio]"),
    dict(oldId=3426776, newId=344560146, relation="rename", ratio=1.0, confidence="high", ratioSource="raw_price_1to1",
         evidence="Waste Connections (US) -> Waste Connections (Progressive Waste renamed) 2016-06-01: WCNX 1:1 (raw 65.47 -> 67.18); old Progressive 59651519 holders 0.4815 (ciqsplit 59651519@2016-06-01)"),
    dict(oldId=59651519, newId=344560146, relation="merger", ratio=0.4815, confidence="high", ratioSource="ciqsplit",
         evidence="Progressive Waste -> Waste Connections 2016-06-01: consolidation 0.4815 (ciqsplit 59651519@2016-06-01 rate 0.4815); raw 31.61 -> 67.18x0.4815 = 32.35"),
    dict(oldId=304662031, newId=548266292, relation="rename", ratio=1.0, confidence="high", ratioSource="stitched_1to1",
         evidence="Liberty Latin America split-off 2018-01-02: LILA tracking stock -> LILA share 1:1 (gvkeyiid; both ids print 21.58 on 2018-01-02)"),
    dict(oldId=115485825, newId=564865668, relation="rename", ratio=1.0, confidence="high", ratioSource="stitched_1to1",
         evidence="GGP -> Brookfield Property REIT 2018-08-28: retained GGP share -> 1 BPR class A (gvkeyiid + events N/C); raw 22.06 -> 20.80; the pro-rata cash $23.50 / BPY-unit election consideration is a ledger leg (R1), not a ratio"),
    dict(oldId=47439862, newId=433968949, relation="rename", ratio=2.1584, confidence="high", ratioSource="ciqsplit",
         evidence="IAC/InterActiveCorp -> Match Group 2020-07-01: 2.1584 new MTCH per old IAC (ciqsplit 47439862@2020-07-01 rate 2.1584; co_acthist SPN) + 1 new IAC (674599883) per old share = reverse spin-off leg (distributions ok row)"),
    # TEVA: NYSE ADR line (securityId 2657568 'ADR(CNV 1 ORD ILS0.10)', 1 ADS = 1 ordinary share) ended 2026-09-11 (status 8);
    # the ordinary-share line 7727657 carries the NYSE listing from 2026-09-14 (volume 4.4M vs <= 1.1k odd-lot prints before).
    dict(oldId=2657569, newId=7727657, relation="relist", ratio=1.0, confidence="high", ratioSource="security_terms", linkClass="relist",
         effectiveDate="2026-09-14",
         evidence="TEVA ADR -> ordinary shares 2026-09-14: ADR(CNV 1 ORD ILS0.10) = 1 ADS per ordinary share; ADR 2657569 last close 37.09 on 2026-09-11 "
                  "(status 8), ordinary 7727657 first liquid close 38.72 on 2026-09-14 (open 37.66, volume 4,381,992); pre-2026-09-14 rows of 7727657 are "
                  "sporadic odd-lot prints (612 rows, 436 O=H=L=C, volume 100-1,100, only 7/609 closes equal to the ADR) -> genuine but not tradable "
                  "(firstTradeDate lifted to 2026-09-14, rows kept). ADR line is an aux instrument (lineage_predecessor)"),
]


def chain_gap_flags(v2: pd.DataFrame, lifecycle: pd.DataFrame, cal: np.ndarray, max_gap_tdays: int = 5) -> pd.DataFrame:
    """Same-line rows (rename/relist) get reviewFlag 'chain_gap=<n>' when more than max_gap_tdays trading days separate the old id's
    lastTradeDate and the new id's firstTradeDate: R2 must not chain the first return across such a gap (first row stays NaN)."""
    k = lifecycle.set_index("tradingItemId")
    out = v2.copy()
    gaps = []
    for r in out.itertuples():
        if r.relation not in ("rename", "relist"):
            gaps.append(np.nan)
            continue
        lo, fn = k.lastTradeDate.get(r.oldId, pd.NaT), k.firstTradeDate.get(r.newId, pd.NaT)
        if pd.isna(lo) or pd.isna(fn):
            gaps.append(np.nan)
            continue
        if fn <= lo:
            gaps.append(0)
            continue
        gaps.append(int(ml.tdays_between(cal, np.array([lo], dtype="datetime64[ns]"), np.array([fn], dtype="datetime64[ns]"))[0]) - 1)
    out["chainGapTradingDays"] = pd.array(gaps, dtype="Int64")
    flag = out.chainGapTradingDays.notna() & (out.chainGapTradingDays > max_gap_tdays)
    # chainable = same-line link R2 may use for the new id's first return: high/medium, ratio known, gap <= max_gap_tdays
    out["chainable"] = (out.relation.isin(["rename", "relist"]) & out.confidence.isin(["high", "medium"]) & out.ratio.notna()
                        & out.chainGapTradingDays.notna() & (out.chainGapTradingDays <= max_gap_tdays)).astype(bool)
    out.loc[flag, "reviewFlag"] = (out.loc[flag, "reviewFlag"].fillna("").astype(str) + " | chain_gap=" + out.loc[flag, "chainGapTradingDays"].astype(str)
                                   + f" trading days between old lastTradeDate and new firstTradeDate (> {max_gap_tdays}): do not chain the first return").str.strip(" |")
    return out


def build_lineage_v2(rc1: pd.DataFrame, reclass: pd.DataFrame, overrides: list[dict] | None = None) -> pd.DataFrame:
    """rc1 lineage rows updated in place by the reclassification (same (oldId, newId) key; relation/ratio/confidence/evidence
    replaced, ratioSource/linkClass/reviewFlag added) and by the explicit overrides; new pairs appended.  Contract columns
    first (LINEAGE_COLS), then the v2 extras."""
    v2 = rc1.copy()
    for c in ("ratioSource", "linkClass", "reviewFlag"):
        v2[c] = ""
    if "sources" not in v2.columns:
        v2["sources"] = ""
    v2["version"] = "v1-rc1"
    key = pd.MultiIndex.from_arrays([v2.oldId.astype("int64"), v2.newId.astype("int64")])
    rows_new = []

    def upsert(old, new, relation, ratio, conf, evidence, ratio_src, link_cls, review, eff=None):
        hit = key.get_indexer([(int(old), int(new))])
        if hit[0] >= 0:
            i = v2.index[hit[0]]
            v2.at[i, "relation"] = relation
            v2.at[i, "ratio"] = ratio
            v2.at[i, "confidence"] = conf
            v2.at[i, "evidence"] = evidence[:1000]
            v2.at[i, "ratioSource"] = ratio_src
            v2.at[i, "linkClass"] = link_cls
            v2.at[i, "reviewFlag"] = review
            v2.at[i, "version"] = LINEAGE_V2_VERSION
            if eff is not None and pd.notna(eff):
                v2.at[i, "effectiveDate"] = pd.Timestamp(eff)
        else:
            rows_new.append(dict(oldId=int(old), newId=int(new), relation=relation, effectiveDate=pd.Timestamp(eff) if eff is not None else pd.NaT,
                                 ratio=ratio, evidence=evidence[:1000], confidence=conf, ratioSource=ratio_src, linkClass=link_cls,
                                 reviewFlag=review, sources="R3", version=LINEAGE_V2_VERSION))

    for r in reclass.itertuples():
        upsert(r.oldId, r.newId, r.relation, r.ratio, r.confidence, "[R3 reclass " + r.linkClass + "] " + r.evidence, r.ratioSource, r.linkClass,
               r.reviewFlag, r.effectiveDate)
    for o in overrides or []:
        upsert(o["oldId"], o["newId"], o["relation"], o["ratio"], o["confidence"], "[R3 first-day link] " + o["evidence"], o["ratioSource"],
               o.get("linkClass", "simple_rename" if o["relation"] == "rename" else o["relation"]), o.get("reviewFlag", ""), o.get("effectiveDate"))
    if rows_new:
        v2 = pd.concat([v2, pd.DataFrame(rows_new)], ignore_index=True)
    if "chainGapTradingDays" not in v2.columns:
        v2["chainGapTradingDays"] = pd.array([pd.NA] * len(v2), dtype="Int64")
    if "chainable" not in v2.columns:
        v2["chainable"] = False
    v2["oldId"] = v2.oldId.astype("int64")
    v2["newId"] = v2.newId.astype("int64")
    v2["effectiveDate"] = mb.to_ns(v2.effectiveDate)
    v2["ratio"] = v2.ratio.astype("float64")
    for c in ("relation", "evidence", "confidence", "ratioSource", "linkClass", "reviewFlag", "sources", "version"):
        v2[c] = v2[c].fillna("").astype(str)
    v2["chainGapTradingDays"] = v2.chainGapTradingDays.astype("Int64")
    v2["chainable"] = v2.chainable.fillna(False).astype(bool)
    v2 = v2[LINEAGE_V2_COLS].sort_values(["oldId", "newId"]).reset_index(drop=True)
    if v2.duplicated(["oldId", "newId"]).any():
        raise RuntimeError("lineage v2 has duplicate (oldId, newId) pairs")
    return v2


# --------------------------------------------------------------------------------------
# rc2 / R3 CLI: reclassify -> lineage v2, events reclass input, R1 hand-over
# --------------------------------------------------------------------------------------
EVENTS_RECLASS_COLS = ["tradingItemId", "successorTradingItemId", "successorRatio", "ratioSource", "linkClass", "confidence", "cashPerShare",
                       "effectiveDate", "evidence"]
R1_COLS = ["tradingItemId", "exDate", "eventType", "legType", "cashPerShare", "cashCurrency", "parentFactor", "childTradingItemId", "childRatio",
           "childValueSource", "source", "evidence", "status", "isEstimate", "notes"]


def r1_handover(reclass: pd.DataFrame, overrides: list[dict]) -> pd.DataFrame:
    """Legs of the reclassified transitions in the BRIEF 3-1 columns R3 can fill (one row per leg), for every class whose
    transition carries more than the successor share (F1): cash legs that are NOT in ciqdividend (manual), the successor leg of
    mergers, and every extra stock leg (child).  Extra stock legs are typed `spin_off`/stock so that the loader export gives them
    an engine path (BRIEF 3-3 excludes merger rows from the engine ledger).  Cash legs that exist in ciqdividend are NOT
    repeated here (R1 builds them from ciqdividend); they are referenced in `notes` (ledgerCashLegRef) so R1/E1 can dedupe
    against the events row's cashPerShare."""
    rows = []
    for r in reclass.itertuples():
        if r.ratioSource in ("none",):
            continue
        has_extra = (isinstance(r.stockLegs, str) and r.stockLegs != "") or (np.isfinite(r.cashLeg) and r.cashLeg > 0)
        if r.linkClass not in ("merger", "spin_off") and not has_extra:
            continue
        ex = pd.Timestamp(r.effectiveDate)
        est = r.ratioSource in ("manual_public_terms", "unresolved", "assumed_1to1")
        status = "unresolved" if r.ratioSource == "unresolved" else ("estimated" if est else "ok")
        src = {"manual_public_terms": "manual_override", "ciqsplit": "ciqsplit", "co_acthist_text": "co_acthist"}.get(r.ratioSource, "lineage")
        cash_ref = f"ledgerCashLegRef: {r.cashLegSource}" if isinstance(r.cashLegSource, str) and "ciqdividend" in r.cashLegSource else ""
        note = (f"R3 reclass {r.linkClass}; events row carries cashPerShare={r.cashLeg if np.isfinite(r.cashLeg) else 0:.6g}; {cash_ref}; {r.reviewFlag}")[:400]
        if r.linkClass == "merger":
            rows.append(dict(tradingItemId=r.oldId, exDate=ex, eventType="merger_stock", legType="stock", cashPerShare=np.nan, cashCurrency="USD",
                             parentFactor=0.0, childTradingItemId=r.newId, childRatio=r.ratio if np.isfinite(r.ratio) else np.nan,
                             childValueSource="child_close_ex" if np.isfinite(r.ratio) else "unknown", source=src if np.isfinite(r.ratio) else "lineage",
                             evidence=r.evidence[:400], status=status if np.isfinite(r.ratio) else "unresolved", isEstimate=est or not np.isfinite(r.ratio), notes=note))
        elif r.linkClass == "spin_off":
            rows.append(dict(tradingItemId=r.oldId, exDate=ex, eventType="spin_off", legType="quantity", cashPerShare=np.nan, cashCurrency="USD",
                             parentFactor=r.ratio if np.isfinite(r.ratio) else np.nan, childTradingItemId=r.newId, childRatio=np.nan,
                             childValueSource="", source=src, evidence=r.evidence[:400], status=status, isEstimate=est,
                             notes=("parent line continues in childTradingItemId (=newId) with parentFactor; " + note)[:400]))
        # manual cash legs (not in ciqdividend): the events row carries them; listed here for the ledger with the dedupe note
        if isinstance(r.cashLegSource, str) and "manual" in r.cashLegSource and np.isfinite(r.cashLeg):
            manual_cash = [float(x.split()[1]) for x in r.cashLegSource.split("; ") if x.startswith("manual")]
            for c in manual_cash:
                rows.append(dict(tradingItemId=r.oldId, exDate=ex, eventType="merger_cash", legType="cash", cashPerShare=c, cashCurrency="USD",
                                 parentFactor=0.0 if r.linkClass == "merger" else 1.0, childTradingItemId=pd.NA, childRatio=np.nan, childValueSource="",
                                 source="manual_override", evidence=r.evidence[:400], status="estimated", isEstimate=True,
                                 notes=("carried by the events row cashPerShare (public deal terms [추정]); ledger must not add it twice; " + note)[:400]))
        # extra stock legs (children): spin_off/stock so the loader exports them to the engine ledger
        for leg in (r.stockLegs.split("; ") if isinstance(r.stockLegs, str) and r.stockLegs else []):
            m = re.match(r"(\d+)x([-0-9.e+nan]+)@([-0-9.e+nan]+)\[(.*)\]", leg)
            if not m:
                continue
            cid, ratio_c, src_c = int(m.group(1)), float(m.group(2)), m.group(4)
            if src_c.startswith("distributions"):
                continue          # already a distributions/ledger row on the same parent (R1 builds it)
            rows.append(dict(tradingItemId=r.oldId, exDate=ex, eventType="spin_off", legType="stock", cashPerShare=np.nan, cashCurrency="USD",
                             parentFactor=1.0, childTradingItemId=cid, childRatio=ratio_c if np.isfinite(ratio_c) else np.nan,
                             childValueSource="child_close_ex" if np.isfinite(ratio_c) else "unknown", source="manual_override",
                             evidence=r.evidence[:400], status="estimated" if np.isfinite(ratio_c) else "unresolved", isEstimate=True,
                             notes=("extra stock leg of the transition (engine path: ledger spin_off on the settlement bar); " + note)[:400]))
    # first-day links that carry a leg for R1 (QRTE.A Liberty Ventures distribution; MTCH new-IAC leg is already a distributions ok row)
    rows.append(dict(tradingItemId=326004249, exDate=pd.Timestamp("2014-10-15"), eventType="spin_off", legType="stock", cashPerShare=np.nan, cashCurrency="USD",
                     parentFactor=1.0, childTradingItemId=215503912, childRatio=0.14217, childValueSource="child_close_ex", source="manual_override",
                     evidence="QVC Group (326004249, first close 2014-10-15) distributed 0.14217 Liberty Ventures Series A (215503912) per share, ciqdividend type 20 ex 2014-10-15 pay 2014-10-20 (distributions row ratio_unknown); explains the -14.6% step from LINTA 27.74 (10-14) to QVCA 23.70 [추정 ratio]",
                     status="estimated", isEstimate=True, notes="R3 first-day link: event falls on the new id's first close -> apply to the chained first return"))
    rows.append(dict(tradingItemId=115485825, exDate=pd.Timestamp("2018-08-28"), eventType="other", legType="cash", cashPerShare=np.nan, cashCurrency="USD",
                     parentFactor=1.0, childTradingItemId=pd.NA, childRatio=np.nan, childValueSource="unknown", source="lineage",
                     evidence="GGP -> Brookfield Property REIT 2018-08-28: pro-rata election consideration ($23.50 cash or 0.9656 BPY unit per elected share) on top of the retained 1:1 BPR share; not in ciqdividend/co_acthist terms",
                     status="unresolved", isEstimate=True, notes="R3: lineage ratio 1.0 covers the retained share only; the partial cash/BPY leg is unpriced here"))
    out = pd.DataFrame(rows, columns=R1_COLS)
    out["childTradingItemId"] = out.childTradingItemId.astype("Int64")
    return out.sort_values(["tradingItemId", "exDate", "eventType"]).reset_index(drop=True)


def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="rc2/R3: reclassify the rejected 1:1 rename rows and assemble lineage v2")
    ap.add_argument("--stage", default=str(mb.SCRATCH / "stage_rc2" / "R3"))
    ap.add_argument("--rejected", default=str(mb.SCRATCH / "stage_rc2" / "R3" / "in" / "qa_T6" / "rejected_rename_rows.csv"))
    ap.add_argument("--lineage", default=str(mb.SCRATCH / "stage_rc2" / "R3" / "in" / "master" / "lineage.parquet"))
    ap.add_argument("--lifecycle", default=str(mb.SCRATCH / "stage_rc2" / "R3" / "in" / "master" / "lifecycle.parquet"))
    ap.add_argument("--t7-class", default=str(mb.SCRATCH / "stage_rc2" / "R3" / "in" / "qa_T7" / "H_rejected_rename_classified.csv"))
    ap.add_argument("--calendar", default=str(mb.SCRATCH / "stage_rc2" / "R3" / "in" / "calendar.parquet"))
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    stage = Path(args.stage)
    (stage / "master").mkdir(parents=True, exist_ok=True)
    (stage / "qa").mkdir(parents=True, exist_ok=True)
    rej = pd.read_csv(args.rejected)
    rej["end"] = pd.to_datetime(rej.end)
    lc = pd.read_parquet(args.lifecycle)
    db = DB(application_name="cache_v21_R3")
    ev = pull_rename_evidence(db, rej, stage / "work" / "_ev", lc)
    db.close()
    t7 = pd.read_csv(args.t7_class) if Path(args.t7_class).exists() else None
    reclass = classify_rename_rejections(rej, ev, lc, t7)
    if t7 is not None:
        k = t7.set_index(["tid", "succ"])
        reclass["everTop500"] = [bool(k.everTop500.get((a, b), False)) for a, b in zip(reclass.oldId, reclass.newId)]
        reclass["everTop1000"] = [bool(k.everTop1000.get((a, b), False)) for a, b in zip(reclass.oldId, reclass.newId)]
    reclass.to_parquet(stage / "qa" / "rename_reclass.parquet", **mb.PARQUET_KW)
    reclass.to_csv(stage / "qa" / "rename_reclass.csv", index=False)
    rc1 = pd.read_parquet(args.lineage)
    v2 = build_lineage_v2(rc1, reclass, LINEAGE_V2_OVERRIDES)
    # lifecycle patch (firstTradeDate lifts implied by v2) + chain-gap flags on the patched lifecycle (+ aux rows when present)
    cal = pd.read_parquet(args.calendar).date.to_numpy(dtype="datetime64[ns]")
    aux_path = stage / "master" / "lifecycle_aux.parquet"
    la = pd.read_parquet(aux_path) if aux_path.exists() else None
    lc_patched, patch, la_patched = ml.lineage_v2_first_date_patch(v2, lc, la, reclass, cal)
    mb.write_parquet(lc_patched, stage / "master" / "lifecycle.parquet")
    if la_patched is not None:
        mb.write_parquet(la_patched, aux_path)
    patch.to_csv(stage / "qa" / "lifecycle_r3_patch.csv", index=False)
    v2 = chain_gap_flags(v2, pd.concat([lc_patched, la_patched]) if la_patched is not None else lc_patched, cal)
    mb.write_parquet(v2, stage / "master" / "lineage.parquet")
    # events reclass input: rows with a structural ratio (not price-implied), for events_build.apply_reclassification
    ok = reclass[reclass.ratio.notna() & ~reclass.ratioSource.isin(["unresolved", "none"])]
    ei = pd.DataFrame({"tradingItemId": ok.oldId, "successorTradingItemId": ok.newId, "successorRatio": ok.ratio, "ratioSource": ok.ratioSource,
                       "linkClass": ok.linkClass, "confidence": ok.confidence, "cashPerShare": ok.cashLeg, "effectiveDate": ok.effectiveDate,
                       "evidence": ok.evidence.str.slice(0, 400),
                       "reviewFlag": np.where(~ok.legsComplete, "partial_terms: " + ok.reviewFlag.str.extract(r"partial_terms: ([^|]*)")[0].fillna(""),
                                              np.where(ok.legsFlag != "", ok.legsFlag, "")),
                       "R_total": ok.R_total})
    ei[EVENTS_RECLASS_COLS + ["reviewFlag", "R_total"]].to_csv(stage / "events_reclass_input.csv", index=False)
    r1 = r1_handover(reclass, LINEAGE_V2_OVERRIDES)
    r1.to_csv(stage / "for_R1_events.csv", index=False)
    summary = {"n_rejected": int(len(rej)), "by_class": reclass.linkClass.value_counts().to_dict(),
               "by_class_ratioSource": reclass.groupby(["linkClass", "ratioSource"]).size().reset_index().rename(columns={0: "n"}).to_dict(orient="records"),
               "by_confidence": reclass.confidence.value_counts().to_dict(),
               "ratio_resolved": int(reclass.ratio.notna().sum()), "ratio_unresolved": int(reclass.ratio.isna().sum()),
               "everTop500_unresolved": int((reclass.ratio.isna() & reclass.get("everTop500", False)).sum()),
               "lineage_v2_rows": int(len(v2)), "lineage_v2_updated": int((v2.version == LINEAGE_V2_VERSION).sum()),
               "lineage_v2_new_pairs": int(len(v2) - len(rc1)), "lineage_v2_chain_gap_flagged": int((v2.chainGapTradingDays.fillna(0) > 5).sum()),
               "lifecycle_patch_rows": int(len(patch)), "events_reclass_rows": int(len(ei)), "r1_rows": int(len(r1)),
               "r1_by_type_status": r1.groupby(["eventType", "status"]).size().reset_index().rename(columns={0: "n"}).to_dict(orient="records"),
               "at": mb.now()}
    mb.save_json(summary, stage / "qa" / "rename_reclass_summary.json")
    log.info("reclass: %s", {k: v for k, v in summary.items() if k not in ("by_class_ratioSource", "r1_by_type_status")})
    return 0


if __name__ == "__main__":
    sys.exit(main())
