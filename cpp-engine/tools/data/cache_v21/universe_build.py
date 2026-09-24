#!/home/jgo/usr/venv-3.11/bin/python
"""cache_v21/universe_build.py - `universe/membership.parquet` + `universe/membership_v1rule.parquet` (T5 steps 2-3).

v1 universe rule (sim/modules/TopUniverse_uss_v1.py, parameters = v1 TopUniverseV1/METADATA.yml) applied to the
raw-basis cache_v2.1 panels:

    dollar      = ts_mean(close, 100) * ts_mean(volume, 100)          close/volume raw as-quoted (option: other bases)
    dvv         = dollar > MinSpotDollarVolume (3e6)
    dv          = marketCap_USD (derived/marketcap.parquet, company cap on priced days)
    dvol_ok     = ts_median(dv, 21) > MinDollarMarketCap (1e7)  &  currentPrimary  &  dvv
    liq         = cs_rank(ts_median(dv, 21)[(dv > 0) & dvol_ok])       (average rank scaled to [0, 1])
    ALL         = sticky(isfinite(liq)) & (dv > 0),  TopK = sticky(liq > (n+1-K)/(n+1)) & (dv > 0)
    sticky      = ffill(limit=StickyExitDays 10) then keep runs longer than StickyEntryDays 60
    TokensToRemove (46 ids) forced False.

Candidate pools (inputs are NaN outside the pool *before* the rolling statistics and the ranking):
    membership_v1rule : inV1Rule items only                             (= prototype TopUniverseV2_v1rule)
    membership        : all items minus the fund screen                 (= prototype TopUniverseV2)
                        fund screen = NOT inV1Rule AND (companyTypeId == 13 OR exchangeId in (117, 467, 358))
    both              : AND tradable (--tradable line|strict|off, default line)
        strict : lifecycle.firstTradeDate <= d <= lastTradeDate & close notna & NOT in master/tradable_exclusions,
                 applied before the rolling statistics/ranking, per id.  A re-listed company restarts the sticky
                 entry count under its new id (61-trading-day gap).
        line   : (lead decision 2026-09-16) same strict tradable per id, but the rule state (rolling dollar volume,
                 market-cap medians, ranks, sticky counters) is computed on *lineage-line* series: for each
                 relist/rename chain (confidence high/medium, transitive) the day's inputs are those of the active id
                 (the member with the latest firstTradeDate <= d, if strictly tradable that day), the line is ranked
                 as one name, and membership is mapped back to that active id.  Ids outside chains are unchanged.
                 No sticky restart at transitions, no double counting (one id per line per day).
        off    : no lifecycle at all (prototype-equivalent pool).
    primary (--primary pit|current, default pit; lead decision 2026-09-16):
        currentPrimary is a *current* flag, so under `strict` the old id of a re-listed company (BLK, GOOGL, AVGO, ...)
        would never be a candidate and the company would vanish for its pre-relist history.  `pit` makes an id eligible
        when it is currentPrimary OR a predecessor (relist/rename chain, confidence high/medium, transitive) of a
        currentPrimary id, up to its own lastTradeDate; the 'Trust|ETF' name filter uses the old id's own name.
        `current` = today's flag only (prototype/v1).
    Gate: no two ids of one lineage line are members on the same day (double counting) -> run.json lineage_duplicates.

Output (BRIEF §3-5): universe str | tradingItemId int64 | fromDate ts[ns] | toDate ts[ns] (inclusive), one row per
maximal run of consecutive member trading days; the inverse transform is asserted bit-identical to the daily panel
(and cross-checked with the reference loader's get_mask).

Runs are named so that QA can build the ablation ladder (see universe_qa.py):
    universe_build.py run --name full            (defaults: tradable line, primary pit, dollar raw_meanxmean, our mcap)
    universe_build.py run --name strict_pit --tradable strict            (per-id lifecycle: sticky restart at re-listings)
    universe_build.py run --name strict_curprim --tradable strict --primary current   (company-vanishing counterfactual)
    universe_build.py run --name notrad --no-tradable --primary current  (prototype-equivalent)
    universe_build.py run --name notrad_sadj --no-tradable --dollar split_adj_meanxmean
    universe_build.py run --name notrad_sadj_pmcap --no-tradable --dollar split_adj_meanxmean --mcap proto
    universe_build.py run --name notrad_entry30 --no-tradable --param StickyEntryDays=30      (counterexample)
Each run writes `T5_work/runs/<name>/{membership.parquet, membership_v1rule.parquet, run.json}`; `--publish` also
copies the two files to `T5/universe/`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from universe_common import (  # noqa: E402
    NS_PER_DAY, PROTO_ROOT, T1_MASTER, T2_ROOT, T5_ROOT, T5_WORK, UNIVERSE_NAMES, Timer, fmt_int, load_current,
    load_exclusions, load_instruments, load_lifecycle, log, rss_gb, setup_logging, t1_v2_ready, to_ns_i8,
    write_json, write_parquet,
)
import build_cache_v2 as bc2  # noqa: E402  (prototype: parameters, helpers; import only)
from loader import CacheV21  # noqa: E402

UNIV_PARAMS = dict(bc2.UNIV_PARAMS)
TOKENS_TO_REMOVE = list(bc2.TOKENS_TO_REMOVE)
FUND_COMPANY_TYPE = bc2.FUND_COMPANY_TYPE
FUND_EXCHANGES = tuple(bc2.FUND_EXCHANGES)
DOLLAR_MODES = ("raw_meanxmean", "split_adj_meanxmean", "raw_product_mean")
assert list(bc2.UNIVERSE_NAMES) == UNIVERSE_NAMES


MIN_DAY_FRAC = 0.2   # prototype --min-day-frac default


def trading_calendar(close: np.ndarray, dates: pd.DatetimeIndex, min_day_frac: float) -> tuple[np.ndarray, pd.DataFrame]:
    """keep(bool T), table(date, close_rows, local_median, keep).  Same rule as build_cache_v2.step_date_index."""
    counts = pd.Series(np.isfinite(close).sum(axis=1).astype(np.int64), index=dates)
    local_med = counts.rolling(21, center=True, min_periods=1).median()
    keep = (counts >= min_day_frac * local_med).to_numpy()
    cal = pd.DataFrame({"date": dates, "close_rows": counts.to_numpy(), "local_median": local_med.to_numpy(), "keep": keep})
    log.info("trading calendar: %d of %d dates kept; dropped %s", int(keep.sum()), len(dates),
             {str(d.date()): int(c) for d, c in zip(dates[~keep], counts.to_numpy()[~keep])})
    return keep, cal


LINEAGE_RELATIONS = ("relist", "rename")
LINEAGE_CONFIDENCE = ("high", "medium")


def lineage_edges(lineage: pd.DataFrame, ids: np.ndarray) -> pd.DataFrame:
    """old->new edges (relist/rename, confidence high/medium) with both ends in the panel."""
    id_index = pd.Index(ids)
    ln = lineage[lineage.relation.isin(LINEAGE_RELATIONS) & lineage.confidence.isin(LINEAGE_CONFIDENCE)]
    ln = ln[ln.oldId.isin(id_index) & ln.newId.isin(id_index) & (ln.oldId != ln.newId)]
    return ln[["oldId", "newId", "relation", "confidence", "effectiveDate"]].drop_duplicates(["oldId", "newId"]).reset_index(drop=True)


def predecessors_of_primary(edges: pd.DataFrame, ids: np.ndarray, current_primary: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(pred bool N, depth int N): id reaches a currentPrimary id by following old->new edges transitively.

    depth = number of edges to the first currentPrimary successor (0 for ids that are themselves currentPrimary)."""
    id_index = pd.Index(ids)
    succ: dict[int, list[int]] = {}
    for o, n in zip(edges.oldId.to_numpy(), edges.newId.to_numpy()):
        succ.setdefault(id_index.get_loc(int(o)), []).append(id_index.get_loc(int(n)))
    pred = np.zeros(ids.size, dtype=bool)
    depth = np.full(ids.size, -1, dtype=np.int64)
    depth[current_primary] = 0
    for c in range(ids.size):
        if current_primary[c] or c not in succ:
            continue
        # BFS over successors (cycles guarded by the visited set)
        frontier, seen, d = list(succ[c]), {c}, 1
        found = -1
        while frontier and d <= 20:
            nxt = []
            for x in frontier:
                if x in seen:
                    continue
                seen.add(x)
                if current_primary[x]:
                    found = d
                    break
                nxt.extend(succ.get(x, []))
            if found >= 0:
                break
            frontier, d = nxt, d + 1
        if found >= 0:
            pred[c] = True
            depth[c] = found
    return pred, depth


def line_groups(edges: pd.DataFrame, ids: np.ndarray) -> np.ndarray:
    """column -> terminal column of its old->new chain (ids without successor map to themselves)."""
    id_index = pd.Index(ids)
    nxt = {id_index.get_loc(int(o)): id_index.get_loc(int(n)) for o, n in zip(edges.oldId.to_numpy(), edges.newId.to_numpy())}
    col = np.arange(ids.size)
    for c in range(ids.size):
        cur, d = c, 0
        while cur in nxt and d < 20:
            cur = nxt[cur]
            d += 1
        col[c] = cur
    return col


def stitched_flags(lc: pd.DataFrame) -> np.ndarray:
    """bool per row of `lc` (indexed by tradingItemId, aligned to the panel ids): firstTradeDate > firstPriceDate."""
    first = lc.firstTradeDate.values.astype("datetime64[ns]")
    fp = lc.firstPriceDate.values.astype("datetime64[ns]") if "firstPriceDate" in lc.columns else first
    fp = np.where(np.isnat(fp), first, fp)
    return fp < first


def stitched_old_map(lc: pd.DataFrame, lineage: pd.DataFrame, ids: np.ndarray, stitched: np.ndarray) -> np.ndarray:
    """column -> column of the lineage old id for stitched ids (-1 = none).  `lc` aligned to `ids`.

    Pick the rename/relist row with effectiveDate == firstTradeDate (T1 lifted it from that row), else the closest."""
    ln = lineage[lineage.relation.isin(["rename", "relist"]) & lineage.effectiveDate.notna()]
    id_index = pd.Index(ids)
    ln = ln[ln.oldId.isin(id_index)]
    first = lc.firstTradeDate.values.astype("datetime64[ns]").astype(np.int64)
    by_new = {k: g for k, g in ln.groupby("newId")}
    old_of = np.full(ids.size, -1, dtype=np.int64)
    for c in np.flatnonzero(stitched):
        g = by_new.get(int(ids[c]))
        if g is None or g.empty:
            continue
        eff = g.effectiveDate.values.astype("datetime64[ns]").astype(np.int64)
        exact = np.flatnonzero(eff == first[c])
        pick = g.iloc[exact[0]] if exact.size else g.iloc[int(np.argmin(np.abs(eff - first[c])))]
        old_of[c] = id_index.get_loc(int(pick.oldId))
    return old_of


# ------------------------------------------------------------------------------------------------------
# Inputs
# ------------------------------------------------------------------------------------------------------
class Inputs:
    """Wide panels (T x N) on the loader's fixed axes plus per-item attributes."""

    def __init__(self, t2_root: Path, master_root: Path, t5_root: Path, need_split_adj: bool,
                 min_day_frac: float = MIN_DAY_FRAC, work: Path = T5_WORK):
        tm = Timer("load panels")
        inst_ids = np.asarray(load_instruments(master_root).tradingItemId, dtype=np.int64)       # rc2 (D2): aux rows in base_data are not candidates
        cv = CacheV21(t2_root, master_root=master_root, universe_root=t5_root, threads=8, validate=False, ids=inst_ids)
        all_dates = cv.dates()
        self.ids = cv.ids()
        close_full = cv.get_panel("close", "raw", as_array=True)          # float32 (all base_data dates)
        # trading calendar (prototype step_date_index rule): drop dates whose close count is below
        # min_day_frac x the centered 21-day median count (single-print US holidays)
        self.keep_rows, cal = trading_calendar(close_full, all_dates, min_day_frac)
        cal.to_parquet(work / "calendar.parquet", index=False)
        self.dropped_dates = cal[~cal.keep]
        self.dates = all_dates[self.keep_rows]
        self.dates_i8 = self.dates.values.astype("datetime64[ns]").astype(np.int64)
        self.all_dates_i8 = all_dates.values.astype("datetime64[ns]").astype(np.int64)
        self.close = close_full[self.keep_rows]
        del close_full
        self.volume = cv.get_panel("volume", "raw", as_array=True)[self.keep_rows]        # float64
        self.close_adj = self.volume_adj = None
        if need_split_adj:
            self.close_adj = cv.get_panel("close", "split_adj", as_array=True)[self.keep_rows]
            self.volume_adj = cv.get_panel("volume", "split_adj", as_array=True)[self.keep_rows]
        cv.release()
        self.cv = cv
        self.T, self.N = self.close.shape
        self.close_fin = np.isfinite(self.close)
        tm.done(f"T={self.T} (of {all_dates.size} base dates; dropped {int((~cal.keep).sum())}) N={self.N}")

        inst = load_instruments(master_root).set_index("tradingItemId").reindex(self.ids)
        cur = load_current(master_root).set_index("tradingItemId").reindex(self.ids)
        if inst.companyId.isna().any() or cur.exchangeId.isna().any():
            raise RuntimeError("panel ids missing from T1 instruments/current")
        self.inst, self.cur = inst, cur
        self.in_v1rule = inst.inV1Rule.to_numpy(dtype=bool)
        self.current_primary = cur.currentPrimary.to_numpy(dtype=bool)
        ct = pd.read_parquet(T5_WORK / "company_types.parquet").drop_duplicates("companyid").set_index("companyid").companytypeid
        ctype = inst.companyId.map(ct)
        not_v1 = ~self.in_v1rule
        self.is_fund_co = (ctype == FUND_COMPANY_TYPE).to_numpy() & not_v1
        self.is_fund_venue = cur.exchangeId.isin(FUND_EXCHANGES).to_numpy() & not_v1
        self.fund_like = self.is_fund_co | self.is_fund_venue
        self.token_cols = np.isin(self.ids, TOKENS_TO_REMOVE)
        log.info("items: %d; inV1Rule %d; currentPrimary %d; fund screen %d (company type %d, venue %d); tokens present %d",
                 self.N, self.in_v1rule.sum(), self.current_primary.sum(), self.fund_like.sum(), self.is_fund_co.sum(),
                 self.is_fund_venue.sum(), self.token_cols.sum())
        # PIT primary eligibility (lead decision 2026-09-16): an id that is not currentPrimary now but is a
        # predecessor (relist/rename chain, confidence high/medium, transitive) of a currentPrimary id is eligible
        # up to its own lifecycle.lastTradeDate; the 'Trust|ETF' name filter applies to the old id's own name.
        self.lineage = pq.read_table(master_root / "lineage.parquet").to_pandas()
        self.edges = lineage_edges(self.lineage, self.ids)
        pred, depth = predecessors_of_primary(self.edges, self.ids, self.current_primary)
        name_ok = ~inst.companyName.fillna("").str.contains(bc2.PRIMARY_NAME_PATTERN, regex=True).to_numpy()
        self.pred_primary = pred & name_ok & ~self.current_primary
        self.pred_depth = depth
        self.line_col = line_groups(self.edges, self.ids)
        lc_ = load_lifecycle(master_root).set_index("tradingItemId").reindex(self.ids)
        self.last_trade_i8 = lc_.lastTradeDate.values.astype("datetime64[ns]").astype(np.int64)
        self.hi_trade_rows = np.searchsorted(self.dates_i8, self.last_trade_i8, side="right")
        first_rows = np.searchsorted(self.dates_i8, lc_.firstTradeDate.values.astype("datetime64[ns]").astype(np.int64), side="left")
        # a predecessor is eligible only until the day before its (earliest) direct successor starts trading, so two
        # ids of one line are never eligible on the same day (double-counting gate); 21/1432 edges overlap otherwise
        id_index = pd.Index(self.ids)
        self.pred_cut_rows = self.hi_trade_rows.copy()
        for o, n in zip(self.edges.oldId.to_numpy(), self.edges.newId.to_numpy()):
            co, cn = id_index.get_loc(int(o)), id_index.get_loc(int(n))
            self.pred_cut_rows[co] = min(self.pred_cut_rows[co], first_rows[cn])
        n_overlap_cut = int((self.pred_primary & (self.pred_cut_rows < self.hi_trade_rows)).sum())
        # TokensToRemove is a list of *current* ids; in v1 (one stitched id per line) it removed the whole line, so
        # under PIT primary the predecessors of a token are removed as well (RTN<-RTN.B, WBD<-DISC.A, GTE<-GDSK.E, FNF<-FNF)
        pred_of: dict[int, list[int]] = {}
        for o, n in zip(self.edges.oldId.to_numpy(), self.edges.newId.to_numpy()):
            pred_of.setdefault(id_index.get_loc(int(n)), []).append(id_index.get_loc(int(o)))
        token_pred = np.zeros(self.N, dtype=bool)
        stack = list(np.flatnonzero(self.token_cols))
        seen = set(stack)
        while stack:
            x = stack.pop()
            for pcol in pred_of.get(x, []):
                if pcol not in seen:
                    seen.add(pcol)
                    token_pred[pcol] = True
                    stack.append(pcol)
        self.token_pred_cols = token_pred
        self.token_cols_effective = self.token_cols | token_pred
        self.tokens_effective = [int(x) for x in self.ids[self.token_cols_effective]]
        log.info("PIT primary: currentPrimary %d, predecessor-of-primary %d (name filter removed %d), lineage edges %d, "
                 "chains depth>1 %d, line groups with >1 id %d, predecessors cut at successor start %d",
                 self.current_primary.sum(), self.pred_primary.sum(), int((pred & ~name_ok & ~self.current_primary).sum()),
                 len(self.edges), int((depth > 1).sum()), int((np.bincount(self.line_col, minlength=self.N) > 1).sum()), n_overlap_cut)
        log.info("TokensToRemove: %d listed (%d in panel) + %d predecessor ids removed with them: %s", len(TOKENS_TO_REMOVE),
                 int(self.token_cols.sum()), int(token_pred.sum()), [int(x) for x in self.ids[token_pred]])

    def primary_ok(self, mode: str) -> np.ndarray:
        """(T,N) bool: close notna & primary eligibility.  mode 'pit' (default) or 'current' (prototype/v1)."""
        if mode == "current":
            return self.close_fin & self.current_primary[None, :]
        if mode != "pit":
            raise ValueError(mode)
        rows = np.arange(self.T)[:, None]
        elig = self.current_primary[None, :] | (self.pred_primary[None, :] & (rows < self.pred_cut_rows[None, :]))
        return self.close_fin & elig

    # ---- market cap
    def mcap_panel(self, source: str, t5_root: Path) -> np.ndarray:
        tm = Timer(f"mcap panel ({source})")
        out = np.full((self.T, self.N), np.nan, dtype=np.float64)
        if source == "ours":
            t = pq.read_table(t5_root / "derived" / "marketcap.parquet", columns=["date", "tradingItemId", "marketCap_USD"])
            d = to_ns_i8(t.column("date").to_numpy())
            i = t.column("tradingItemId").to_numpy().astype(np.int64)
            v = t.column("marketCap_USD").to_numpy(zero_copy_only=False).astype(np.float64)
            r = np.searchsorted(self.dates_i8, d)
            id_sorted = np.argsort(self.ids, kind="stable")
            c_pos = np.searchsorted(self.ids[id_sorted], i)
            ok = (r < self.T) & (c_pos < self.N)
            ok &= self.dates_i8[np.minimum(r, self.T - 1)] == d
            ok &= self.ids[id_sorted][np.minimum(c_pos, self.N - 1)] == i
            if not ok.all():
                log.info("marketcap rows outside the calendar/axes (dropped dates): %d", int((~ok).sum()))
            out[r[ok], id_sorted[c_pos[ok]]] = v[ok]
        elif source == "proto":
            t = pq.read_table(PROTO_ROOT / "BaseData" / "marketCap_USD.parquet")
            pdates = to_ns_i8(t.column("__index_level_0__").to_numpy())
            r = np.searchsorted(self.dates_i8, pdates)
            okr = (r < self.T)
            okr &= self.dates_i8[np.minimum(r, self.T - 1)] == pdates
            id_index = pd.Index(self.ids)
            n_cols = 0
            for name in t.column_names:
                if name == "__index_level_0__":
                    continue
                c = id_index.get_loc(int(name)) if int(name) in id_index else -1
                if c < 0:
                    continue
                v = t.column(name).to_numpy(zero_copy_only=False).astype(np.float64)
                out[r[okr], c] = v[okr]
                n_cols += 1
            log.info("proto mcap: %d columns mapped, %d dates matched of %d", n_cols, int(okr.sum()), pdates.size)
        else:
            raise ValueError(source)
        out[~self.close_fin] = np.nan          # v1 meaning: cap only on priced days
        tm.done(f"finite cells {fmt_int(np.isfinite(out).sum())}")
        return out

    # ---- tradable mask (T x N): lifecycle window & close notna & not excluded
    def tradable_mask(self, master_root: Path) -> tuple[np.ndarray, np.ndarray, dict]:
        """(strict, line, info).

        strict = lifecycle.firstTradeDate <= d <= lastTradeDate & close notna & not excluded   (T4 loader 'tradable')
        line   = same but the window of a *stitched* id starts at firstPriceDate (diagnostic only: counts the
                 vendor-stitched cells; the rule never uses vendor-stitched history, see build_lines)."""
        lc = load_lifecycle(master_root).set_index("tradingItemId").reindex(self.ids)
        if lc.firstTradeDate.isna().any() or lc.lastTradeDate.isna().any():
            raise RuntimeError("lifecycle missing firstTradeDate/lastTradeDate for panel ids")
        first = lc.firstTradeDate.values.astype("datetime64[ns]").astype(np.int64)
        last = lc.lastTradeDate.values.astype("datetime64[ns]").astype(np.int64)
        if "firstPriceDate" in lc.columns:
            fp = lc.firstPriceDate.values.astype("datetime64[ns]")
            first_price = np.where(np.isnat(fp), first, fp.astype(np.int64))
        else:
            first_price = first
        self.lo_trade = np.searchsorted(self.dates_i8, first, side="left")
        self.lo_price = np.searchsorted(self.dates_i8, np.minimum(first_price, first), side="left")
        self.hi_trade = np.searchsorted(self.dates_i8, last, side="right")
        self.stitched = self.lo_price < self.lo_trade
        rows = np.arange(self.T)[:, None]
        window = (rows >= self.lo_trade[None, :]) & (rows < self.hi_trade[None, :])
        strict = window & self.close_fin
        n_tail = int((self.close_fin & (rows >= self.hi_trade[None, :])).sum())
        n_stitched_cells = int((self.close_fin & (rows < self.lo_trade[None, :]) & (rows >= self.lo_price[None, :])).sum())
        excl = load_exclusions(master_root)
        exm = np.zeros((self.T, self.N), dtype=bool)
        n_excl_rows = 0
        if excl is not None and len(excl):
            id_index = pd.Index(self.ids)
            for r_ in excl.itertuples(index=False):
                if r_.tradingItemId not in id_index:
                    continue
                c = id_index.get_loc(r_.tradingItemId)
                f = pd.Timestamp(r_.fromDate).value if pd.notna(r_.fromDate) else self.dates_i8[0]
                t_ = pd.Timestamp(r_.toDate).value if pd.notna(r_.toDate) else self.dates_i8[-1]
                s = np.searchsorted(self.dates_i8, f, side="left")
                e = np.searchsorted(self.dates_i8, t_, side="right")
                exm[s:e, c] = True
                n_excl_rows += 1
        n_excl_cells = int((strict & exm).sum())
        strict &= ~exm
        line = (rows >= self.lo_price[None, :]) & (rows < self.hi_trade[None, :]) & self.close_fin & ~exm
        self.strict_tradable = strict
        self.line_tradable = line
        # old id of every stitched id (lineage rename/relist whose effectiveDate == lifted firstTradeDate)
        old_of = stitched_old_map(lc, pq.read_table(master_root / "lineage.parquet").to_pandas(), self.ids, self.stitched)
        n_no_old = int((self.stitched & (old_of < 0)).sum())
        self.old_of = old_of
        info = {"priced_cells": int(self.close_fin.sum()), "tradable_cells_strict": int(strict.sum()),
                "tradable_cells_line": int(line.sum()),
                "priced_after_lastTradeDate": n_tail, "priced_stitched_before_firstTradeDate": n_stitched_cells,
                "stitched_ids": int(self.stitched.sum()), "stitched_ids_without_old_in_panel": n_no_old,
                "exclusion_rows_applied": n_excl_rows, "exclusion_cells_removed": n_excl_cells,
                "lifecycle_file_mtime": dt.datetime.fromtimestamp((master_root / "lifecycle.parquet").stat().st_mtime).isoformat(timespec="seconds"),
                "t1_done_v2": t1_v2_ready()}
        log.info("tradable mask: %s", info)
        return strict, line, info

    # ---- line series (lead decision 2026-09-16 17:50): universe state is computed per lineage line
    def build_lines(self) -> dict:
        """For every lineage line with >= 2 ids: the active id per trading day.

        active(d) = the chain member with the latest firstTradeDate <= d (the successor represents the line from its
        firstTradeDate on; the old id represents it before), provided that member is strictly tradable on d
        (lifecycle window, close notna, not excluded); otherwise the line has no data that day (-1).  The line is
        carried by the terminal id's column during the rule; membership is mapped back to active(d)."""
        if not hasattr(self, "strict_tradable"):
            raise RuntimeError("tradable_mask() must run before build_lines()")
        cnt = np.bincount(self.line_col, minlength=self.N)
        lines = {int(t_): sorted(np.flatnonzero(self.line_col == t_).tolist(), key=lambda c: (self.lo_trade[c], c))
                 for t_ in np.flatnonzero(cnt > 1)}
        act: dict[int, np.ndarray] = {}
        n_days_active, n_switch = 0, 0
        for t_, mem in lines.items():
            a = np.full(self.T, -1, dtype=np.int64)
            for m in mem:
                a[self.lo_trade[m]:] = m
            rows = np.flatnonzero(a >= 0)
            ok = self.strict_tradable[rows, a[rows]]
            a[rows[~ok]] = -1
            act[t_] = a
            n_days_active += int((a >= 0).sum())
            n_switch += len(mem) - 1
        self.lines, self.line_act = lines, act
        info = {"lines": len(lines), "ids_in_lines": int(sum(len(m) for m in lines.values())), "transitions": n_switch,
                "line_active_cells": n_days_active}
        log.info("lines built: %s", info)
        return info

    def to_line_panel(self, panel: np.ndarray, fill) -> np.ndarray:
        """Copy of `panel` where each line's terminal column carries the active id's values and the other members
        are `fill` (they do not take part separately)."""
        out = panel.copy()
        for t_, mem in self.lines.items():
            a = self.line_act[t_]
            rows = np.flatnonzero(a >= 0)
            col = np.full(self.T, fill, dtype=panel.dtype)
            col[rows] = panel[rows, a[rows]]
            for m in mem:
                out[:, m] = fill
            out[:, t_] = col
        return out

    def from_line_masks(self, masks: dict[str, np.ndarray]) -> dict:
        """Map each line's (terminal-column) membership back to the active id of the day; assert members ⊆ strict."""
        stats = {}
        for u, m in masks.items():
            moved = 0
            for t_, mem in self.lines.items():
                a = self.line_act[t_]
                vals = m[:, t_].copy()
                for mm in mem:
                    m[:, mm] = False
                rows = np.flatnonzero(vals & (a >= 0))
                m[rows, a[rows]] = True
                moved += int(rows.size)
                lost = int((vals & (a < 0)).sum())          # member on a day without an active id (cannot happen: dv NaN)
                if lost:
                    log.warning("%s line %d: %d member days without active id dropped", u, self.ids[t_], lost)
            bad = int((m & ~self.strict_tradable).sum())
            if bad:
                raise RuntimeError(f"{u}: {bad} member cells outside the strict tradable mask after line mapping")
            stats[u] = {"line_member_days_mapped": moved, "members_not_strict_tradable_after": bad}
        return stats


# ------------------------------------------------------------------------------------------------------
# Rule
# ------------------------------------------------------------------------------------------------------
def universe_rule(close, volume, mcap, prim_ok, cand, ids, tokens, params, dollar_mode="raw_meanxmean",
                  close_adj=None, volume_adj=None, return_pre: bool = False):
    """Port of TopUniverse_uss_v1 with a (T,N) candidate mask.  Returns {name: bool (T,N)}.

    cand=None means every cell; otherwise inputs are NaN outside `cand` *before* rolling statistics/ranking.
    Helpers (rolling_mean/median, cs_rank_rows, make_sticky) are the prototype's (build_cache_v2)."""
    t0 = time.time()
    rm, rmed, rank, sticky = bc2.rolling_mean, bc2.rolling_median, bc2.cs_rank_rows, bc2.make_sticky

    def masked(x):
        return x if cand is None else np.where(cand, x, np.nan)

    if dollar_mode == "raw_meanxmean":
        dollar = rm(masked(close), params["DollarValueWindow"]) * rm(masked(volume), params["DollarValueWindow"])
    elif dollar_mode == "split_adj_meanxmean":
        dollar = rm(masked(close_adj), params["DollarValueWindow"]) * rm(masked(volume_adj), params["DollarValueWindow"])
    elif dollar_mode == "raw_product_mean":
        dollar = rm(masked(close.astype(np.float64) * volume), params["DollarValueWindow"])
    else:
        raise ValueError(dollar_mode)
    dvv = dollar > params["MinSpotDollarVolume"]
    del dollar
    dv = masked(mcap).astype(np.float64, copy=False)
    med_f = rmed(dv, params["FilterWindow"])
    med_r = med_f if params["RankWindow"] == params["FilterWindow"] else rmed(dv, params["RankWindow"])
    sdv = prim_ok if cand is None else (prim_ok & cand)
    mcap_ok = med_f > params["MinDollarMarketCap"]
    dvol_ok = mcap_ok & sdv & dvv
    sel = (dv > 0) & dvol_ok
    liq = rank(np.where(sel, med_r, np.nan))
    pre = {"dvv": dvv, "mcap_ok": mcap_ok, "prim_cand": sdv} if return_pre else None
    del med_f, med_r, dvv, dvol_ok, sel, mcap_ok
    dv_pos = dv > 0
    if return_pre:
        pre["dv_pos"] = dv_pos
    del dv
    token_cols = np.isin(ids, list(tokens))
    out = {}
    univ = np.isfinite(liq)
    if return_pre:
        pre["sel_ALL"] = univ.copy()
    u = sticky(univ, params["StickyExitDays"], params["StickyEntryDays"]) & dv_pos
    u[:, token_cols] = False
    out["ALL"] = u
    n_fin = np.isfinite(liq).sum(axis=1)
    for name in UNIVERSE_NAMES[1:]:
        usize = int(name[3:])
        thr = np.maximum(0, n_fin + 1 - usize) / (n_fin + 1)
        univ = liq > thr[:, None]
        if return_pre:
            pre[f"sel_{name}"] = univ.copy()
        u = sticky(univ, params["StickyExitDays"], params["StickyEntryDays"]) & dv_pos
        u[:, token_cols] = False
        out[name] = u
    log.info("universe rule (%s, cand=%s): med names(last 2000d) %s (%.0fs, rss %.1f GB)", dollar_mode,
             "all" if cand is None else f"{int(cand.sum()):,} cells",
             {k: int(np.median(v.sum(axis=1)[-2000:])) for k, v in out.items()}, time.time() - t0, rss_gb())
    return (out, pre) if return_pre else out


# ------------------------------------------------------------------------------------------------------
# Intervals <-> daily panel
# ------------------------------------------------------------------------------------------------------
def mask_to_intervals(mask: np.ndarray, dates_i8: np.ndarray, ids: np.ndarray, universe: str) -> pd.DataFrame:
    """Maximal runs of consecutive True trading days per column -> rows (universe, tradingItemId, fromDate, toDate)."""
    T, N = mask.shape
    m = np.zeros((N, T + 2), dtype=np.int8)
    m[:, 1:T + 1] = mask.T
    d = np.diff(m, axis=1)                    # (N, T+1): +1 at run start (index t), -1 after run end
    cs, ts = np.nonzero(d == 1)
    ce, te = np.nonzero(d == -1)
    if cs.size != ce.size or not np.array_equal(cs, ce):
        raise RuntimeError("run start/end mismatch")
    return pd.DataFrame({"universe": universe, "tradingItemId": ids[cs].astype(np.int64),
                         "fromDate": pd.to_datetime(dates_i8[ts]), "toDate": pd.to_datetime(dates_i8[te - 1])})


def intervals_to_mask(df: pd.DataFrame, dates_i8: np.ndarray, ids: np.ndarray) -> np.ndarray:
    """Inverse of mask_to_intervals for one universe (rows of `df`); toDate inclusive."""
    T, N = dates_i8.size, ids.size
    order = np.argsort(ids, kind="stable")
    ids_sorted = ids[order]
    tid = df.tradingItemId.to_numpy(np.int64)
    pos = np.searchsorted(ids_sorted, tid)
    posc = np.minimum(pos, N - 1)
    found = ids_sorted[posc] == tid
    if not found.all():
        raise KeyError(f"{int((~found).sum())} interval ids not in panel")
    col = order[posc]
    s = np.searchsorted(dates_i8, df.fromDate.values.astype("datetime64[ns]").astype(np.int64), side="left")
    e = np.searchsorted(dates_i8, df.toDate.values.astype("datetime64[ns]").astype(np.int64), side="right")
    diff = np.zeros((T + 1, N), dtype=np.int32)
    np.add.at(diff, (s, col), 1)
    np.add.at(diff, (e, col), -1)
    return np.cumsum(diff[:-1], axis=0) > 0


def membership_table(masks: dict[str, np.ndarray], dates_i8: np.ndarray, ids: np.ndarray) -> pd.DataFrame:
    parts = [mask_to_intervals(masks[name], dates_i8, ids, name) for name in UNIVERSE_NAMES]
    df = pd.concat(parts, ignore_index=True)
    df["universe"] = df.universe.astype(str)
    df = df.sort_values(["universe", "tradingItemId", "fromDate"],
                        key=lambda s: s.map({n: i for i, n in enumerate(UNIVERSE_NAMES)}) if s.name == "universe" else s)
    return df.reset_index(drop=True)


def roundtrip_selftest(masks: dict[str, np.ndarray], df: pd.DataFrame, dates_i8: np.ndarray, ids: np.ndarray) -> dict:
    """(1) planted-difference detection, (2) bit-identical inverse for every universe."""
    res = {}
    name = "Top500"
    rec = intervals_to_mask(df[df.universe == name], dates_i8, ids)
    planted = rec.copy()
    c = int(np.flatnonzero(planted.any(axis=0))[0])
    t = int(np.flatnonzero(planted[:, c])[0])
    planted[t, c] = False
    res["counterexample_detected"] = bool(not np.array_equal(planted, masks[name]))
    res["bit_identical"] = {}
    for u in UNIVERSE_NAMES:
        rec = intervals_to_mask(df[df.universe == u], dates_i8, ids)
        res["bit_identical"][u] = bool(np.array_equal(rec, masks[u]))
    res["all_pass"] = res["counterexample_detected"] and all(res["bit_identical"].values())
    return res


def to_arrow(df: pd.DataFrame) -> pa.Table:
    return pa.table({
        "universe": pa.array(df.universe.astype(str).tolist(), type=pa.string()),
        "tradingItemId": pa.array(df.tradingItemId.to_numpy(np.int64), type=pa.int64()),
        "fromDate": pa.array(df.fromDate.values.astype("datetime64[ns]")),
        "toDate": pa.array(df.toDate.values.astype("datetime64[ns]")),
    })


# ------------------------------------------------------------------------------------------------------
# Run
# ------------------------------------------------------------------------------------------------------
TRADABLE_MODES = ("line", "strict", "off")


def transition_diagnostics(inputs: "Inputs", masks: dict, pre: dict, cand: np.ndarray, line_mode: bool,
                           universes=("Top500", "Top1000", "ALL")) -> pd.DataFrame:
    """Per lineage edge old->new (both in panel): membership and pre-sticky eligibility around the transition.

    t_first = new.firstTradeDate row, t_last = row before (or old's last row if earlier).  `pre_*` are the rule's
    pre-sticky selections read from the column that carried the line (terminal id in line mode, the id itself
    otherwise).  A *transition-caused* gap is: old member on t_last & new not member on t_first & line pre-eligible
    on both days (nothing in the rule inputs explains the drop)."""
    entry = int(UNIV_PARAMS["StickyEntryDays"]) + 1
    rows = []
    id_index = pd.Index(inputs.ids)
    for r in inputs.edges.itertuples(index=False):
        co, cn = id_index.get_loc(int(r.oldId)), id_index.get_loc(int(r.newId))
        t_first = int(inputs.lo_trade[cn])
        t_last = int(min(inputs.hi_trade[co], t_first)) - 1
        if t_last < 0 or t_first >= inputs.T or t_first <= t_last:
            continue
        col_last = inputs.line_col[co] if line_mode else co
        col_first = inputs.line_col[cn] if line_mode else cn
        base = {"oldId": int(r.oldId), "newId": int(r.newId), "relation": r.relation, "confidence": r.confidence,
                "effectiveDate": pd.Timestamp(r.effectiveDate), "t_last": t_last, "t_first": t_first,
                "date_last": inputs.dates[t_last], "date_first": inputs.dates[t_first],
                "data_gap_days": int(max(inputs.lo_trade[cn] - inputs.hi_trade[co], 0)),
                "new_in_pool_first": bool(cand[t_first, col_first]), "old_in_pool_last": bool(cand[t_last, col_last]),
                "dvv_first": bool(pre["dvv"][t_first, col_first]), "mcap_ok_first": bool(pre["mcap_ok"][t_first, col_first]),
                "dv_pos_first": bool(pre["dv_pos"][t_first, col_first]), "dvv_last": bool(pre["dvv"][t_last, col_last]),
                "mcap_ok_last": bool(pre["mcap_ok"][t_last, col_last])}
        for u in universes:
            m = masks[u]
            hi = min(t_first + entry, inputs.T)
            seg = m[t_first:hi, cn] | m[t_first:hi, co]
            first_new = np.flatnonzero(m[t_first:, cn])
            rows.append({**base, "universe": u, "old_member_last": bool(m[t_last, co]), "new_member_first": bool(m[t_first, cn]),
                         "pre_last": bool(pre[f"sel_{u}"][t_last, col_last]), "pre_first": bool(pre[f"sel_{u}"][t_first, col_first]),
                         "gap_days_first_61": int((~seg).sum()),
                         "new_first_member_row": int(t_first + first_new[0]) if first_new.size else -1,
                         "old_member_days": int(m[:, co].sum()), "new_member_days": int(m[:, cn].sum())})
    return pd.DataFrame(rows)


def lineage_duplicates(mask: np.ndarray, line_col: np.ndarray, rows_sel: np.ndarray | None = None) -> dict:
    """Cells where two ids of the same lineage line are members on the same day (double counting).  0 expected."""
    groups = np.flatnonzero(np.bincount(line_col, minlength=line_col.size) > 1)
    total, pairs = 0, 0
    worst = None
    m = mask if rows_sel is None else mask[rows_sel]
    for g in groups:
        cols = np.flatnonzero(line_col == g)
        cnt = m[:, cols].sum(axis=1)
        dup = int(np.maximum(cnt - 1, 0).sum())
        if dup:
            pairs += 1
            total += dup
            if worst is None or dup > worst[1]:
                worst = (int(g), dup)
    return {"dup_cells": total, "lines_with_dups": pairs, "lines_checked": int(groups.size), "worst_line_col": worst}


def run(name: str, inputs: Inputs, master_root: Path, t5_root: Path, work: Path, tradable: str, dollar_mode: str,
        mcap_source: str, params: dict, variants=("v1rule", "all"), publish: bool = False,
        loader_check: bool = True, primary: str = "pit") -> dict:
    """tradable: 'line' (default): strict tradable per id, but the rule (rolling stats, ranks, sticky counters) runs
    on lineage-line series (active id's inputs per day) and membership is mapped back to the active id;
    'strict': per-id lifecycle window before ranking (re-listed lines restart the sticky entry count);
    'off': no lifecycle (prototype-equivalent)."""
    if tradable not in TRADABLE_MODES:
        raise ValueError(tradable)
    run_dir = work / "runs" / name
    run_dir.mkdir(parents=True, exist_ok=True)
    t_run = time.time()
    mcap = inputs.mcap_panel(mcap_source, t5_root)
    prim_ok = inputs.primary_ok(primary)
    trad_info = None
    trad = None
    line_info = None
    if tradable != "off":
        strict, _line_unused, trad_info = inputs.tradable_mask(master_root)
        trad = strict
        del _line_unused
        if tradable == "line":
            line_info = inputs.build_lines()
            close_in = inputs.to_line_panel(inputs.close, np.nan)
            volume_in = inputs.to_line_panel(inputs.volume, np.nan)
            mcap_in = inputs.to_line_panel(mcap, np.nan)
            prim_in = inputs.to_line_panel(prim_ok, False)
            close_adj_in = inputs.to_line_panel(inputs.close_adj, np.nan) if inputs.close_adj is not None else None
            volume_adj_in = inputs.to_line_panel(inputs.volume_adj, np.nan) if inputs.volume_adj is not None else None
    if tradable != "line":
        close_in, volume_in, mcap_in, prim_in = inputs.close, inputs.volume, mcap, prim_ok
        close_adj_in, volume_adj_in = inputs.close_adj, inputs.volume_adj
    info = {"name": name, "tradable": tradable, "primary": primary, "dollar_mode": dollar_mode, "mcap_source": mcap_source,
            "params": params, "tokens": len(TOKENS_TO_REMOVE), "tradable_info": trad_info,
            "pit_primary": {"currentPrimary": int(inputs.current_primary.sum()), "predecessor_of_primary": int(inputs.pred_primary.sum()),
                            "lineage_edges": int(len(inputs.edges)), "tokens_in_panel": int(inputs.token_cols.sum()),
                            "token_predecessors_removed": [int(x) for x in inputs.ids[inputs.token_pred_cols]] if primary == "pit" else []},
            "lines": line_info,
            "T": inputs.T, "N": inputs.N, "dates": [str(inputs.dates[0].date()), str(inputs.dates[-1].date())],
            "calendar": {"base_dates": int(inputs.keep_rows.size), "kept": int(inputs.keep_rows.sum()),
                         "dropped": {str(pd.Timestamp(d).date()): int(c) for d, c in
                                     zip(inputs.dropped_dates.date, inputs.dropped_dates.close_rows)}},
            "variants": {}, "started": dt.datetime.now().isoformat(timespec="seconds")}
    for variant in variants:
        allowed_cols = inputs.in_v1rule if variant == "v1rule" else ~inputs.fund_like
        cand = np.broadcast_to(allowed_cols[None, :], (inputs.T, inputs.N))
        cand = (cand & trad) if trad is not None else np.ascontiguousarray(cand)
        if tradable == "line":
            cand = inputs.to_line_panel(cand, False)          # pool membership of the day's active id
        tokens = inputs.tokens_effective if primary == "pit" else TOKENS_TO_REMOVE
        masks, pre = universe_rule(close_in, volume_in, mcap_in, prim_in, cand, inputs.ids, tokens, params,
                                   dollar_mode, close_adj_in, volume_adj_in, return_pre=True)
        reassign = None
        if tradable == "line":
            reassign = inputs.from_line_masks(masks)
            log.info("line -> active id mapping (%s): %s", variant, {u: v for u, v in reassign.items() if u in ("Top500", "ALL")})
        elif tradable == "strict":
            bad = {u: int((m & ~inputs.strict_tradable).sum()) for u, m in masks.items()}
            if any(bad.values()):
                raise RuntimeError(f"members outside strict tradable: {bad}")
        if tradable != "off":
            tdiag = transition_diagnostics(inputs, masks, pre, cand, line_mode=(tradable == "line"))
            tdiag.to_parquet(run_dir / f"transitions_{variant}.parquet", index=False)
            caused = tdiag[tdiag.old_member_last & ~tdiag.new_member_first & tdiag.pre_last & tdiag.pre_first]
            log.info("transition diagnostics (%s): edges %d; old member on last day %s; transition-caused gaps %s", variant,
                     tdiag.oldId.nunique(), tdiag.groupby("universe").old_member_last.sum().to_dict(),
                     caused.groupby("universe").size().to_dict() if len(caused) else {})
        del pre
        dups = {u: lineage_duplicates(masks[u], inputs.line_col) for u in ("Top500", "Top1000", "ALL")}
        log.info("lineage double-counting gate (%s): %s", variant, dups)
        df = membership_table(masks, inputs.dates_i8, inputs.ids)
        st = roundtrip_selftest(masks, df, inputs.dates_i8, inputs.ids)
        if not st["all_pass"]:
            raise RuntimeError(f"interval round-trip self-test failed: {st}")
        fname = "membership.parquet" if variant == "all" else "membership_v1rule.parquet"
        winfo = write_parquet(to_arrow(df), run_dir / fname, row_group_size=1_000_000)
        sel = inputs.dates >= pd.Timestamp("2010-01-01")
        counts = {u: {"med_2010": float(np.median(masks[u][sel].sum(axis=1))),
                      "med_last250": float(np.median(masks[u][-250:].sum(axis=1))),
                      "name_days": int(masks[u].sum()), "items": int(masks[u].any(axis=0).sum()),
                      "rows": int((df.universe == u).sum())} for u in UNIVERSE_NAMES}
        info["variants"][variant] = {"file": str(run_dir / fname), "rows": winfo["rows"], "bytes": winfo["bytes"],
                                     "candidate_items": int(allowed_cols.sum()), "candidate_cells": int(cand.sum()),
                                     "roundtrip_selftest": st, "counts": counts, "line_mapping": reassign,
                                     "lineage_duplicates": dups}
        # daily member counts (small) for QA plots/tables
        cnt = pd.DataFrame({u: masks[u].sum(axis=1).astype(np.int32) for u in UNIVERSE_NAMES}, index=inputs.dates)
        cnt.index.name = "date"
        cnt.reset_index().to_parquet(run_dir / f"daily_counts_{variant}.parquet", index=False)
        del masks, cand
    del mcap, prim_ok, trad, close_in, volume_in, mcap_in, prim_in
    info["wall_s"] = round(time.time() - t_run, 1)
    info["peak_rss_gb"] = round(rss_gb(), 2)
    if publish:
        (t5_root / "universe").mkdir(parents=True, exist_ok=True)
        for variant in variants:
            fname = "membership.parquet" if variant == "all" else "membership_v1rule.parquet"
            shutil.copy2(run_dir / fname, t5_root / "universe" / fname)
        info["published"] = str(t5_root / "universe")
        if loader_check:
            info["loader_check"] = loader_crosscheck(inputs, t5_root, run_dir)
    write_json(run_dir / "run.json", info)
    log.info("run %s done in %.0fs: %s", name, info["wall_s"], {v: info["variants"][v]["counts"]["Top500"] for v in variants})
    return info


def loader_crosscheck(inputs: Inputs, t5_root: Path, run_dir: Path) -> dict:
    """Reference loader reads T5/universe/membership.parquet: its get_mask must equal our inverse transform."""
    cv = CacheV21(inputs.cv.root, master_root=inputs.cv.master_root, universe_root=t5_root, threads=8, validate=False, ids=inputs.ids)
    df = pq.read_table(t5_root / "universe" / "membership.parquet").to_pandas()
    res = {}
    for u in ("Top500", "ALL"):
        m_loader = cv.get_mask(f"universe:{u}", as_array=True)          # on the full base_data calendar
        m_ours = intervals_to_mask(df[df.universe == u], inputs.dates_i8, inputs.ids)
        res[u] = bool(np.array_equal(m_loader[inputs.keep_rows], m_ours))
        res[f"{u}_member_cells_on_dropped_dates"] = int(m_loader[~inputs.keep_rows].sum())
    log.info("loader cross-check (get_mask on calendar rows == intervals_to_mask): %s", res)
    return res


def parse_params(items: list[str]) -> dict:
    p = dict(UNIV_PARAMS)
    for it in items or []:
        k, v = it.split("=")
        if k not in p:
            raise KeyError(k)
        p[k] = type(p[k])(float(v)) if isinstance(p[k], int) else float(v)
    return p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["run"])
    ap.add_argument("--name", required=True)
    ap.add_argument("--t2", type=Path, default=T2_ROOT)
    ap.add_argument("--master", type=Path, default=T1_MASTER)
    ap.add_argument("--t5", type=Path, default=T5_ROOT)
    ap.add_argument("--work", type=Path, default=T5_WORK)
    ap.add_argument("--tradable", choices=TRADABLE_MODES, default="line",
                    help="line (default): strict tradable per id, rule computed on lineage-line series and mapped back to the "
                         "active id (no sticky restart at re-listings); strict: per-id lifecycle window before ranking; off: no lifecycle")
    ap.add_argument("--primary", choices=["pit", "current"], default="pit",
                    help="pit (default): currentPrimary OR predecessor of a currentPrimary id via relist/rename chain (until its "
                         "lastTradeDate); current: today's flag only (prototype/v1)")
    ap.add_argument("--no-tradable", action="store_true", help="alias of --tradable off")
    ap.add_argument("--dollar", choices=DOLLAR_MODES, default="raw_meanxmean")
    ap.add_argument("--mcap", choices=["ours", "proto"], default="ours")
    ap.add_argument("--param", action="append", default=[], help="override, e.g. StickyEntryDays=30")
    ap.add_argument("--variants", default="v1rule,all")
    ap.add_argument("--publish", action="store_true", help="copy the two membership files to T5/universe/")
    args = ap.parse_args(argv)
    setup_logging(args.work / "build.log")
    mode = "off" if args.no_tradable else args.tradable
    log.info("==== universe_build run %s: tradable=%s primary=%s dollar=%s mcap=%s params=%s", args.name, mode,
             args.primary, args.dollar, args.mcap, args.param)
    params = parse_params(args.param)
    inputs = Inputs(args.t2, args.master, args.t5, need_split_adj=(args.dollar == "split_adj_meanxmean"))
    run(args.name, inputs, args.master, args.t5, args.work, tradable=mode, dollar_mode=args.dollar,
        mcap_source=args.mcap, params=params, variants=tuple(args.variants.split(",")), publish=args.publish,
        primary=args.primary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
