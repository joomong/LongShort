#!/home/jgo/usr/venv-3.11/bin/python
"""thin-v1 캐시 빌더 (단순화 로직 단계 T1, orch/BRIEF_S.md §3-1·§6, TASKS.md T1·T1 재작업).

rc2f 발행본을 읽어 엔진이 `전일 position × returns_total` + 이동 행만으로 손익을 내도록 얇은 캐시를 만든다.
returns_total 은 현행 엔진(src/engine/simulator.cpp settle/book_settlements/book_corporate_actions/value_holdings)이
1 단위 보유에 기장하는 값과 같게 계산한다. 엔진 입력 패널 = loader.CacheV21.get_panel('close','split_adj',cut='lifecycle').

  thin_build.py --source <발행본> --out <thin 폴더> --work <작업 폴더>

출력(out): manifest.json calendar.parquet base_data.parquet adj_factor.parquet returns.parquet(total return,
           신호 기본값·보유 손익) adj_returns.parquet(분할조정 가격 수익률) events.parquet universe.parquet master.parquet
작업(work): transitional.csv(date, tradingItemId, kind, detail, value_used), v2_diff.csv, build_log.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))
from loader import CacheV21  # noqa: E402

NS_PER_DAY = 86_400_000_000_000
WRITE = dict(compression="zstd", use_dictionary=False, write_statistics=True, version="2.6")
TRANSITIONAL_KINDS = ("explicit_file_return", "default_settlement", "pending_child")
V2_DIFF_TOL = 1e-9
PRECISION_TOL = 1e-6        # 절대: |thin − v2| ≤ 1e-6 → precision(건수만 기록)
VALUE_SOURCES_WITHOUT_QUOTE = ("wi_close", "event_value", "vendor_implied")   # 엔진 usable_without_quote


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def _day(ts) -> int:
    """timestamp → 일 번호(자정 내림)."""
    return int(pd.Timestamp(ts).value // NS_PER_DAY)


def _write(table: pa.Table, path: Path) -> None:
    pq.write_table(table.replace_schema_metadata(None), path, **WRITE)


def _last_trade(source: Path) -> pd.Series:
    """tradingItemId → lastTradeDate 일 번호(NaT = 절단 없음, 없는 id 도 절단 없음). loader._cut_mask 와 같은 규칙."""
    lc = pd.read_parquet(source / "master" / "lifecycle.parquet", columns=["tradingItemId", "lastTradeDate"])
    lt = pd.to_datetime(lc["lastTradeDate"])
    days = np.where(lt.isna(), np.iinfo(np.int64).max, lt.values.astype("datetime64[ns]").astype(np.int64) // NS_PER_DAY)
    return pd.Series(days, index=lc["tradingItemId"].astype(np.int64).to_numpy())


def _cut_keep(ids: np.ndarray, dates_ns: np.ndarray, last: pd.Series) -> np.ndarray:
    lim = last.reindex(ids).fillna(np.iinfo(np.int64).max).to_numpy(dtype=np.float64)
    return (dates_ns // NS_PER_DAY) <= lim


# ------------------------------------------------------------------------------------------------------
# 1 단위 보유 기장 (현행 엔진 규칙)
# ------------------------------------------------------------------------------------------------------
class Panel:
    """엔진 입력 close 패널(float32, T×N)과 관측 판정."""

    def __init__(self, dates_ns: np.ndarray, ids: np.ndarray, close: np.ndarray):
        self.dates_ns, self.ids, self.close = dates_ns, ids, close
        self.days = dates_ns // NS_PER_DAY
        self.T, self.N = close.shape
        self.col = {int(x): i for i, x in enumerate(ids)}
        self.obs = np.isfinite(close) & (close > 0)       # 엔진 valid_close
        # prev[t, i] = t 직전 마지막 관측 bar(엔진 marks_), 없으면 -1
        last = np.where(self.obs, np.arange(self.T, dtype=np.int32)[:, None], np.int32(-1))
        np.maximum.accumulate(last, axis=0, out=last)
        self.prev = np.full_like(last, -1)
        self.prev[1:] = last[:-1]

    def bar_after(self, day: int) -> int:
        """day 다음 첫 bar(엔진 upper_bound(day_end_ms)). 없으면 T."""
        return int(np.searchsorted(self.days, day, side="right"))

    def first_silent(self, i: int, start: int) -> int:
        """start 이상에서 시세가 처음 없는 bar, 없으면 -1."""
        if start >= self.T:
            return -1
        miss = np.flatnonzero(~self.obs[start:, i])
        return start + int(miss[0]) if miss.size else -1

    def mark(self, i: int, bar: int) -> float:
        """bar 직전 마지막 관측 종가. 없으면 NaN."""
        k = int(self.prev[bar, i])
        return float(self.close[k, i]) if k >= 0 else float("nan")

    def price(self, i: int, bar: int) -> float:
        return float(self.close[bar, i])

    def iso(self, bar: int) -> str:
        return pd.Timestamp(int(self.dates_ns[bar])).strftime("%Y-%m-%d")


def compute(p: Panel, events: pd.DataFrame, ledger: pd.DataFrame) -> dict:
    """1 단위 보유의 returns_total(T×N, NaN = 행 없음), events 행, transitional 행, 통계.

    한 셀의 값 = 정산 R → 현금 leg → 주식 leg → 가격 `A/m − 1` 순서로 0.0 부터 더한다(엔진 pnl 순서).
    """
    T, N = p.T, p.N
    # ── events 행(엔진 read_events) ──
    ev = {}
    for r in events.itertuples(index=False):
        i = p.col.get(int(r.tradingItemId))
        if i is None:
            continue
        start = p.bar_after(_day(r.endDate)) if pd.notna(r.endDate) else T
        succ = p.col.get(int(r.successorTradingItemId)) if pd.notna(r.successorTradingItemId) else None
        ev[i] = dict(row=r, s=p.first_silent(i, start), succ=None if succ == i else succ,
                     cash_leg=bool(r.hasCashLeg) if pd.notna(r.hasCashLeg) else False,
                     stock_leg=bool(r.hasStockLeg) if pd.notna(r.hasStockLeg) else False)

    # ── 원장 leg(엔진 read_corporate_actions 적재 규칙, (bar, parent) 안정 정렬) ──
    first_day, last_day = int(p.days[0]), int(p.days[-1])
    legs = []
    for r in ledger.itertuples(index=False):
        par = p.col.get(int(r.tradingItemId))
        if par is None or r.legType == "quantity":
            continue
        exd = _day(r.exDate)
        if exd < first_day or exd > last_day:
            continue
        b = int(np.searchsorted(p.days, exd))
        if p.days[b] != exd:
            raise ValueError(f"ledger ex-date not a panel bar: {r.tradingItemId} {r.exDate}")
        stock = r.legType in ("stock", "rights")
        child = None
        if stock and pd.notna(r.childTradingItemId) and str(r.childTradingItemId) != "":
            child = p.col.get(int(r.childTradingItemId))
            if child == par:
                raise ValueError(f"ledger row names the parent as its own child: {r.tradingItemId} {r.exDate}")
        expiry_bar = p.bar_after(_day(r.expiryDate)) if r.legType == "rights" and pd.notna(r.expiryDate) else -1
        legs.append(dict(r=r, bar=b, par=par, stock=stock, child=child, expiry_bar=expiry_bar,
                         usable=stock and r.childValueSource in VALUE_SOURCES_WITHOUT_QUOTE))
    legs.sort(key=lambda lg: (lg["bar"], lg["par"]))

    # ── 종료 bar: events 정산, 권리 만료(첫 무시세 bar, mark 있음). events 가 같은 bar·이전이면 우선 ──
    term = {i: (x["s"], "event") for i, x in ev.items() if x["s"] >= 0}
    rights_expiry = {}
    for lg in legs:
        c, e = lg["child"], lg["expiry_bar"]
        if lg["r"].legType == "rights" and c is not None and e >= 0:
            rights_expiry[c] = min(e, rights_expiry.get(c, e))
    for c, e in rights_expiry.items():
        x = p.first_silent(c, e)
        if x >= 0 and p.prev[x, c] >= 0 and not (c in term and term[c][0] <= x):
            term[c] = (x, "expiry")

    def retired(i, bar):
        return i in term and term[i][0] <= bar

    trans, out_ev, excluded = [], [], []
    stats = dict(settle_rows=0, computed=0, explicit_file_return=0, default_settlement=0, last_close=0, successor_rows=0,
                 expiry_rows=0, child_rows=0, child_rows_on_settlement_bar=0, pending_child=0,
                 pending_ledger_value=0, pending_first_quote=0, value_only_legs=0, legs_deferred_to_events=0,
                 legs_on_settlement_bar=0, legs_without_mark=0, frozen_leg_rows=0, settle_identity_max_absdiff=0.0)

    # ── 정산 R ──
    settle = {}
    for i, (s, kind) in term.items():
        m = p.mark(i, s)
        priced_mark = np.isfinite(m) and m > 0.0
        dest, vr = None, None
        if kind == "expiry":
            R = -1.0
            stats["expiry_rows"] += 1
        else:
            x = ev[i]
            r = x["row"]
            succ = x["succ"]
            quoted_succ = succ is not None and not retired(succ, s) and bool(p.obs[s, succ])
            ratio, cash = float(r.successorRatioCache), float(r.cashPerShareCache)
            transferable = quoted_succ and np.isfinite(ratio) and priced_mark
            stock_value = ratio * p.price(succ, s) if transferable else 0.0
            cash_value = cash if (x["cash_leg"] and np.isfinite(cash)) else 0.0
            stock_share = stock_value / m if priced_mark else 0.0
            cash_share = cash_value / m if priced_mark else 0.0
            computable = priced_mark and (x["stock_leg"] or x["cash_leg"]) and \
                (not x["stock_leg"] or transferable) and (not x["cash_leg"] or np.isfinite(cash))
            if computable:
                R = stock_share + cash_share - 1.0
                stats["computed"] += 1
                stats["settle_identity_max_absdiff"] = max(stats["settle_identity_max_absdiff"],
                                                           abs((1.0 + R) - (cash_share + stock_share)))
                if x["stock_leg"] and transferable and stock_value + cash_value > 0.0:
                    dest, vr = succ, stock_share
                    stats["successor_rows"] += 1
            elif np.isfinite(r.settlementReturn):
                R = float(np.float32(r.settlementReturn))          # 엔진은 settlementReturn 을 float32 로 읽는다
                stats["explicit_file_return"] += 1
                trans.append(dict(date=p.iso(s), tradingItemId=int(p.ids[i]), kind="explicit_file_return",
                                  detail=f"status={r.cacheUnitStatus} succ_quoted={quoted_succ} mark={m!r}", value_used=R))
            elif r.cacheUnitStatus == "last_close":
                R = 0.0                                          # override-confirmed last-close settlement, not transitional
                stats["last_close"] += 1
            else:
                R = 0.0
                stats["default_settlement"] += 1
                trans.append(dict(date=p.iso(s), tradingItemId=int(p.ids[i]), kind="default_settlement",
                                  detail=f"status={r.cacheUnitStatus} stock_leg={x['stock_leg']} cash_leg={x['cash_leg']}",
                                  value_used=R))
        settle[i] = R
        out_ev.append(dict(eventId=f"{p.ids[i]}_{p.iso(s).replace('-', '')}_{'expiry' if kind == 'expiry' else 'end'}",
                           date=int(p.dates_ns[s]), sourceId=int(p.ids[i]),
                           destinationId=None if dest is None else int(p.ids[dest]), valueRatio=vr, sourceEnds=True))
        stats["settle_rows"] += 1

    # ── 원장 leg: bar 마다 현금 → 주식(엔진 book_corporate_actions 순서) ──
    cash_terms, stock_terms = {}, {}
    ledger_marked = set()

    def engine_resolution(lg, b, m):
        """FirstQuote 경로: 엔진이 대기 leg 를 푸는 bar 와 값."""
        c, r = lg["child"], lg["r"]
        for di in range(b + 1, T):
            if r.legType == "rights" and lg["expiry_bar"] >= 0 and di >= lg["expiry_bar"]:
                return di, 0.0, "expired"
            if retired(c, di):
                return di, 0.0, "child_retired"
            if p.obs[di, c]:
                return di, r.childRatioCache * p.price(c, di) / m, "first_quote"
        return -1, 0.0, "pending_at_end"

    by_bar = {}
    for lg in legs:
        by_bar.setdefault(lg["bar"], []).append(lg)
    for b, blegs in by_bar.items():
        for phase in ("cash", "stock"):
            for lg in blegs:
                if lg["stock"] != (phase == "stock"):
                    continue
                par, r = lg["par"], lg["r"]
                settling = par in term and term[par][0] == b
                if retired(par, b) and not settling:
                    continue
                m = p.mark(par, b)
                if not (np.isfinite(m) and m > 0.0):
                    stats["legs_without_mark"] += 1
                    continue
                e = ev.get(par)
                if settling:
                    stats["legs_on_settlement_bar"] += 1
                key = (b, par)
                if phase == "cash":
                    if settling and e is not None and e["cash_leg"]:
                        stats["legs_deferred_to_events"] += 1
                        continue
                    if np.isfinite(r.cashPerShareCache):
                        cash_terms.setdefault(key, []).append(r.cashPerShareCache / m)
                    continue
                if settling and e is not None and e["succ"] is not None and lg["child"] == e["succ"]:
                    stats["legs_deferred_to_events"] += 1
                    continue
                c = lg["child"]
                if c is None:
                    v = r.childValuePerShareCache
                    if lg["usable"] and np.isfinite(v) and v > 0.0:         # value-only: 행 없음
                        stock_terms.setdefault(key, []).append(v / m)
                        stats["value_only_legs"] += 1
                    continue
                if not np.isfinite(r.childRatioCache) or retired(c, b):
                    continue
                if p.obs[b, c]:
                    w = r.childRatioCache * p.price(c, b) / m               # 엔진 child_value(1, ratio, price, mark)
                    stock_terms.setdefault(key, []).append(w)
                    out_ev.append(dict(eventId=f"{r.eventId}:{p.ids[c]}", date=int(p.dates_ns[b]), sourceId=int(p.ids[par]),
                                       destinationId=int(p.ids[c]), valueRatio=w, sourceEnds=False))
                    stats["child_rows"] += 1
                    stats["child_rows_on_settlement_bar"] += int(settling)
                    continue
                # 대기 자회사: 이동 행 없음(D7 개정). 엔진 경로를 엔진 처리 순서대로 판정한다.
                stats["pending_child"] += 1
                cclose = r.childCloseAtExCache
                has_est = lg["usable"] and np.isfinite(cclose) and cclose > 0.0
                if has_est and p.prev[b, c] < 0 and c not in ledger_marked:
                    ledger_marked.add(c)
                    thin_v = r.childRatioCache * cclose / m
                    path, eng_bar, eng_v = "ledger_value", b, thin_v
                    stats["pending_ledger_value"] += 1
                    fq = np.flatnonzero(p.obs[b:, c])
                    if fq.size:                                              # 엔진은 첫 시세 bar 를 원장값 mark 로 평가
                        excluded.append((b + int(fq[0]), c, "ledger_value_child_first_quote"))
                else:
                    thin_v = r.childRatioCache * cclose / m if has_est else 0.0
                    eng_bar, eng_v, path = engine_resolution(lg, b, m)
                    stats["pending_first_quote"] += 1
                    excluded.append((b, par, "first_quote_ex_date"))
                    if eng_bar >= 0:
                        excluded.append((eng_bar, par, "first_quote_resolution"))
                stock_terms.setdefault(key, []).append(thin_v)
                trans.append(dict(date=p.iso(b), tradingItemId=int(p.ids[par]), kind="pending_child",
                                  detail=f"child={p.ids[c]} leg={r.eventId} engine_path={path} "
                                         f"engine_date={p.iso(eng_bar) if eng_bar >= 0 else 'none'} engine_value={eng_v!r} "
                                         f"thin_value={thin_v!r} settlement_bar={settling}",
                                  value_used=thin_v))

    # ── 셀 값: 가격(벡터) + 정산·leg(순서대로) ──
    t_idx = np.arange(T)[:, None]
    term_bar = np.full(N, T, dtype=np.int64)
    for i, (s, _) in term.items():
        term_bar[i] = s
    rt = np.full((T, N), np.nan)
    cells = p.obs & (p.prev >= 0) & (t_idx < term_bar[None, :])
    ri, ci = np.nonzero(cells)
    rt[ri, ci] = p.close[ri, ci].astype(np.float64) / p.close[p.prev[ri, ci], ci].astype(np.float64) - 1.0
    del ri, ci, cells
    for key in sorted(set(cash_terms) | set(stock_terms) | {(s, i) for i, (s, _) in term.items()}):
        b, i = key
        v = 0.0
        if i in term and term[i][0] == b:
            v += settle[i]
        elif not p.obs[b, i]:
            stats["frozen_leg_rows"] += 1
        for x in cash_terms.get(key, []):
            v += x
        for x in stock_terms.get(key, []):
            v += x
        price = rt[b, i]
        rt[b, i] = v + price if np.isfinite(price) else v
    return dict(rt=rt, events=out_ev, transitional=trans, stats=stats, excluded=excluded,
                leg_cells=set(cash_terms) | set(stock_terms), term_cells={(s, i) for i, (s, _) in term.items()})


# ------------------------------------------------------------------------------------------------------
# v2 비교표(데이터 단계 인계)
# ------------------------------------------------------------------------------------------------------
def v2_diff(source: Path, cv: CacheV21, p: Panel, rt: np.ndarray, leg_cells: set, term_cells: set, last: pd.Series) -> pd.DataFrame:
    v2 = pq.read_table(source / "derived" / "returns_total.parquet",
                       columns=["date", "tradingItemId", "ret", "prevDate", "retFlag"])
    d = v2.column("date").cast(pa.int64()).to_numpy()
    ids = v2.column("tradingItemId").to_numpy().astype(np.int64)
    r = v2.column("ret").to_numpy(zero_copy_only=False).astype(np.float64)
    prevd = v2.column("prevDate").cast(pa.int64()).to_numpy(zero_copy_only=False)
    flag = v2.column("retFlag").to_numpy()
    del v2
    keep = _cut_keep(ids, d, last)
    d, ids, r, prevd, flag = d[keep], ids[keep], r[keep], prevd[keep], flag[keep]
    row = np.searchsorted(p.dates_ns, d)
    col = np.searchsorted(p.ids, ids)
    thin = rt[row, col]
    v2_has = np.zeros(rt.shape, dtype=bool)
    v2_has[row, col] = True
    bad = np.where(np.isfinite(r) & np.isfinite(thin), np.abs(thin - r) > V2_DIFF_TOL, np.isfinite(r) != np.isfinite(thin))
    k = np.flatnonzero(bad)
    df = pd.DataFrame({"date": d[k], "tradingItemId": ids[k], "thin": thin[k], "v2": r[k], "v2_retFlag": flag[k],
                       "row": row[k], "col": col[k], "prevDate": prevd[k]})
    # thin 에만 있는 행(정산·만료 제외): 무시세 ex-date leg 행
    only = np.isfinite(rt) & ~v2_has & ~p.obs
    orow, ocol = np.nonzero(only)
    term_like = np.array([(b, i) in term_cells for b, i in zip(orow, ocol)], dtype=bool)
    orow, ocol = orow[~term_like], ocol[~term_like]
    df = pd.concat([df, pd.DataFrame({"date": p.dates_ns[orow], "tradingItemId": p.ids[ocol], "thin": rt[orow, ocol],
                                      "v2": np.nan, "v2_retFlag": -1, "row": orow, "col": ocol,
                                      "prevDate": np.iinfo(np.int64).min})], ignore_index=True)
    # 범주
    frozen = set()
    for b, i in leg_cells:
        if not p.obs[b, i]:
            frozen.add((b, i))
            nxt = np.flatnonzero(p.obs[b:, i])
            if nxt.size:
                frozen.add((b + int(nxt[0]), i))
    raw = pd.read_parquet(source / "events" / "ca_ledger.parquet", columns=["tradingItemId", "exDate", "exportExclude"])
    raw_key = raw.assign(k=raw.tradingItemId.astype(np.int64) * 100000 + raw.exDate.values.astype(np.int64) // NS_PER_DAY)
    excl = raw_key.groupby("k")["exportExclude"].max()
    cat = np.full(len(df), "other", dtype=object)
    diff = np.abs(df.thin.to_numpy() - df.v2.to_numpy())
    is_frozen = np.array([(b, i) in frozen for b, i in zip(df.row, df["col"])], dtype=bool)
    is_leg = np.array([(b, i) in leg_cells for b, i in zip(df.row, df["col"])], dtype=bool)
    link = df.thin.isna().to_numpy() & np.isfinite(df.v2.to_numpy())
    dk = df.tradingItemId.to_numpy(np.int64) * 100000 + df.date.to_numpy(np.int64) // NS_PER_DAY
    ex = excl.reindex(dk)
    div = (df.v2_retFlag.to_numpy() > 0) & ~is_leg
    split_def = np.zeros(len(df), dtype=bool)
    cand = np.flatnonzero(~is_frozen & ~link & ~div & (df.prevDate.to_numpy() != np.iinfo(np.int64).min) & ~(diff <= PRECISION_TOL))
    if cand.size:
        f_now = cv.factor_at("split", df.tradingItemId.to_numpy()[cand], pd.to_datetime(df.date.to_numpy()[cand]))
        f_prev = cv.factor_at("split", df.tradingItemId.to_numpy()[cand], pd.to_datetime(df.prevDate.to_numpy()[cand]))
        split_def[cand] = f_now != f_prev
    cat[diff <= PRECISION_TOL] = "precision"
    cat[split_def] = "v2_split_defect"
    sub = np.where(ex.isna().to_numpy(), "not_in_raw_ledger", np.where(ex.eq(True).to_numpy(), "stitched_copy", "export_excluded"))
    cat[div] = np.char.add("v2_only_dividend:", sub[div].astype(str))
    cat[link] = "new_id_link"
    cat[is_frozen] = "frozen_exdate_leg"
    df["diff"] = df.thin - df.v2
    df["category"] = cat
    df["date"] = pd.to_datetime(df.date)
    return df.sort_values(["tradingItemId", "date"], kind="stable")[
        ["date", "tradingItemId", "thin", "v2", "v2_retFlag", "diff", "category"]]


# ------------------------------------------------------------------------------------------------------
# 빌드
# ------------------------------------------------------------------------------------------------------
def build(source, out, work) -> dict:
    source, out, work = Path(source), Path(out), Path(work)
    out.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    last = _last_trade(source)
    cv = CacheV21(source, threads=8)
    dates_ns = cv.dates().values.astype(np.int64)
    ids = cv.ids()
    close = cv.get_panel("close", "split_adj", as_array=True, cut="lifecycle")
    cv.release()
    p = Panel(dates_ns, ids, close)
    log = {"panel": [int(p.T), int(p.N)]}

    # adj_returns (D3, 분할조정 가격 수익률): 전 거래일 대비 분할조정 종가 변화율, 둘 다 있을 때만. (id, date) 정렬.
    fin = np.isfinite(close)
    both = np.zeros_like(fin)
    both[1:] = fin[1:] & fin[:-1]
    ret = np.full(close.shape, np.nan)
    ret[1:] = close[1:].astype(np.float64) / close[:-1].astype(np.float64) - 1.0
    ci, ri = np.nonzero(both.T)
    _write(pa.table({"date": pa.array(dates_ns[ri], pa.timestamp("ns")),
                     "tradingItemId": pa.array(ids[ci], pa.int64()),
                     "adj_returns": pa.array(ret.T[both.T], pa.float64())}), out / "adj_returns.parquet")
    log["adj_returns_rows"] = int(ci.size)
    del ret, both, fin, ci, ri

    # returns = total return (1 단위 보유 기장, 신호 기본값·보유 손익) + events
    res = compute(p, pd.read_parquet(source / "engine" / "events_cache.parquet"),
                  pd.read_parquet(source / "engine" / "ca_ledger_cache.parquet"))
    log["moves"] = res["stats"]
    rt = res["rt"]
    ci, ri = np.nonzero(np.isfinite(rt.T))
    _write(pa.table({"date": pa.array(dates_ns[ri], pa.timestamp("ns")), "tradingItemId": pa.array(ids[ci], pa.int64()),
                     "returns": pa.array(rt.T[ci, ri], pa.float64())}), out / "returns.parquet")
    log["returns_rows"] = int(ci.size)
    del ci, ri

    # v2 비교표
    vd = v2_diff(source, cv, p, rt, res["leg_cells"], res["term_cells"], last)
    log["v2_diff"] = {k: int(v) for k, v in vd.category.value_counts().sort_index().items()}
    vd[vd.category != "precision"].to_csv(work / "v2_diff.csv", index=False, float_format="%.17g")
    log["pending_excluded_cells"] = [(p.iso(b), int(p.ids[i]), why) for b, i, why in sorted(res["excluded"])]
    del vd, rt, close, p

    evdf = pd.DataFrame(res["events"], columns=["eventId", "date", "sourceId", "destinationId", "valueRatio", "sourceEnds"])
    evdf = evdf.sort_values(["date", "sourceId", "eventId"], kind="stable")
    _write(pa.table({
        "eventId": pa.array(evdf["eventId"].tolist(), pa.string()),
        "date": pa.array(evdf["date"].to_numpy(np.int64), pa.timestamp("ns")),
        "sourceId": pa.array(evdf["sourceId"].to_numpy(np.int64), pa.int64()),
        "destinationId": pa.array([None if pd.isna(x) else int(x) for x in evdf["destinationId"]], pa.int64()),
        "valueRatio": pa.array([None if pd.isna(x) else float(x) for x in evdf["valueRatio"]], pa.float64()),
        "sourceEnds": pa.array(evdf["sourceEnds"].astype(bool).tolist(), pa.bool_())}), out / "events.parquet")

    tr = pd.DataFrame(res["transitional"], columns=["date", "tradingItemId", "kind", "detail", "value_used"])
    tr = tr.sort_values(["date", "tradingItemId", "kind", "detail"], kind="stable")
    tr.to_csv(work / "transitional.csv", index=False, float_format="%.17g")
    counts = {k: int((tr["kind"] == k).sum()) for k in TRANSITIONAL_KINDS}

    # base_data (raw, cut) + marketCap_USD
    base = pq.read_table(source / "base_data.parquet",
                         columns=["date", "tradingItemId", "open", "high", "low", "close", "vwap", "volume"]).to_pandas()
    keep = _cut_keep(base["tradingItemId"].to_numpy(np.int64), base["date"].values.astype(np.int64), last)
    log["base_rows"], log["base_rows_cut_removed"] = int(keep.size), int((~keep).sum())
    base = base.loc[keep]
    mc = pq.read_table(source / "derived" / "marketcap.parquet", columns=["date", "tradingItemId", "marketCap_USD"]).to_pandas()
    base = base.merge(mc, on=["date", "tradingItemId"], how="left", sort=False)
    del mc
    base = base.sort_values(["tradingItemId", "date"], kind="stable")
    cols = {"date": pa.timestamp("ns"), "tradingItemId": pa.int64(), "open": pa.float32(), "high": pa.float32(),
            "low": pa.float32(), "close": pa.float32(), "vwap": pa.float32(), "volume": pa.float64(),
            "marketCap_USD": pa.float64()}
    _write(pa.table({c: pa.array(base[c].to_numpy(), t) for c, t in cols.items()}), out / "base_data.parquet")
    log["base_data_rows"] = int(len(base))
    del base

    # adj_factor, universe, master(+aux), calendar
    sp = pq.read_table(source / "factors" / "split.parquet", columns=["tradingItemId", "validFrom", "validTo", "factor"]).to_pandas()
    sp = sp.sort_values(["tradingItemId", "validFrom"], kind="stable")
    _write(pa.Table.from_pandas(sp, preserve_index=False), out / "adj_factor.parquet")
    un = pq.read_table(source / "universe" / "membership.parquet").to_pandas()
    _write(pa.Table.from_pandas(un[["universe", "tradingItemId", "fromDate", "toDate"]], preserve_index=False),
           out / "universe.parquet")
    cur = pd.read_parquet(source / "master" / "current.parquet", columns=["tradingItemId", "symbol"])
    ins = pd.read_parquet(source / "master" / "instruments.parquet", columns=["tradingItemId", "companyName"])
    ms = ins.merge(cur, on="tradingItemId", how="outer")
    aux_path = source / "master" / "aux_instruments.parquet"
    if aux_path.exists():
        aux = pd.read_parquet(aux_path, columns=["tradingItemId", "symbol", "companyName"])
        ms = pd.concat([ms, aux[~aux.tradingItemId.isin(ms.tradingItemId)]], ignore_index=True)
    ms = ms.sort_values("tradingItemId", kind="stable")
    _write(pa.Table.from_pandas(ms[["tradingItemId", "symbol", "companyName"]], preserve_index=False),
           out / "master.parquet")
    log["master_rows"] = int(len(ms))
    shutil.copyfile(source / "calendar.parquet", out / "calendar.parquet")

    names = ["calendar.parquet", "base_data.parquet", "adj_factor.parquet", "returns.parquet", "adj_returns.parquet",
             "events.parquet", "universe.parquet", "master.parquet"]
    src_manifest = json.loads((source / "manifest.json").read_text()) if (source / "manifest.json").exists() else {}
    manifest = {"layout": "thin-v1", "source_build_id": src_manifest.get("build_id"),
                "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "files": {n: _sha256(out / n) for n in names}, "transitional": counts}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    log["events_rows"] = int(len(evdf))
    log["transitional"] = counts
    log["seconds"] = round(time.perf_counter() - t0, 1)
    (work / "build_log.json").write_text(json.dumps(log, indent=1, default=float) + "\n")
    return log


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--work", required=True)
    a = ap.parse_args(argv)
    log = build(a.source, a.out, a.work)
    log.pop("pending_excluded_cells", None)
    print(json.dumps(log, indent=1, default=float))
    return 0


if __name__ == "__main__":
    sys.exit(main())
