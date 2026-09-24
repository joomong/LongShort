#!/home/jgo/usr/venv-3.11/bin/python
"""cache_v21/ledger_build.py - R1: 기업행동 원장 `events/ca_ledger.parquet` (raw 단위, long, rc2 BRIEF §3-1).

한 행 = (부모 tradingItemId, exDate, legType, childTradingItemId). 모든 금액·비율은 **그 날 raw(as-traded) 단위**,
구주(사건 직전 1주) 기준이며 세전(P1)이다. 같은 날 여러 leg(현금+주식, 분할+스핀)는 여러 행이고, 같은 날 행은 전부
사건 직전 구주 1주 기준이므로 수익률 생성기는 그 날 cash/child 를 먼저 합산한 뒤 parentFactor 를 곱한다.

원천(전부 rc1 발행본 + xfeed 읽기 전용 세션):
  cash_dividend / special_dividend : dividends/dividends_long.parquet(ciqdividend). 세전 = grossAmount 우선(세후 감지 규칙은
                                     ledger_repairs.gross_first), 없으면 divAmount. 같은 (id, exDate) 의 현금 행은 합산해 1 행.
  split / stock_dividend           : factors/split.parquet 구간 경계 전수(parentFactor = factor_prev / factor_next) + ciqsplit 증거.
  spin_off / rights / warrant      : distributions/distributions.parquet(kind=stock) ∪ ciqdividend type 20/31 ∪ 수동 교정.
                                     자회사 가치 = P10 순서 ① 교부비율 × 자회사 exDate raw 종가(base_data) ② when-issued(원천 없음)
                                     ③ 사건별 평가액(co_acthist cash equivalent) ④ 벤더 implied(ciqdividendcache 스핀 값·divadj 스텝,
                                     부모 배당락 가격 갭으로 교차 확인된 것만). ③④ 는 isEstimate.
  merger_cash / merger_stock       : events/corporate_actions.parquet(hasCashLeg/hasStockLeg, actionCode != N/C).
  rename / relist                  : master/lineage.parquet(relation rename/relist, parentFactor = ratio).
교정(ledger_repairs.py): 세후 벤더값 → 세전, 배당계수 스케일 오류 55종목 실제 사건 대조, 분할 인공물 97건 실제 분할비·효력일 확인,
TYC 옛 라인 두 leg, HON/FNF/BDX/CTRA/EXAS/EA/GME 수동 교정(GS 관측, BRIEF §2). 교정 로그 events/ca_ledger_repairs.csv.

사건 귀속 규약(rc2 재작업 F4): ciq 가 승계 id 에 과거를 이어붙여 같은 사건이 lineage 로 연결된 두 id 에 기재되므로, 현금·주식·권리
leg 는 **exDate 에 거래되던 id(lifecycle firstTradeDate ≤ exDate ≤ lastTradeDate)** 에만 귀속한다. 거래되지 않던 id 의 복사본은
계보 상대 id 가 같은 사건을 가지면 버리고(ca_ledger_dropped_duplicates.csv, reason history_appended_to_successor), 없으면 거래되던
계보 id 로 옮긴다(notes reassigned_from). 분할(quantity) 행은 id 별 계수 테이블의 사실이라 양쪽 id 에 남긴다. **전환일 leg**(옛 id 마지막
거래일 뒤 7 일 안, 다중 leg 합병의 자회사 leg 포함)는 리드 결정에 따라 **옛 id 에 구주 기준으로** 남기고 승계 id 의 복사본을 버린다
(엔진은 행 id 의 전일 가중치로 leg 를 계상; R2 는 링크된 첫 행에 옛 id 의 갭 사건을 먼저 적용).
키(F1): (tradingItemId, exDate, legType, childTradingItemId) 는 merger_* 행을 제외한 행에서 유일하며, merger 행은 eventType 으로
구분된다(같은 날 현금배당 + 현금 합병 대가는 두 행; 금액이 같은 것만 진짜 중복으로 제거).
이어붙인 과거의 복사본(리드 결정 2026-09-17): 거래 id(keeper)에 귀속된 사건을 승계 id 의 이어붙인 과거 행에도 **stitched_copy 행**으로
남긴다(같은 증권의 과거라 그 구간 수익률·신호 워밍업이 경제적으로 맞도록). leg·값·status·isEstimate 는 keeper 와 동일, source=stitched_copy,
keeperEventId=keeper 의 eventId, exportExclude=true(엔진은 거래 id 만 보유하므로 export 에서 제외). 전환일 leg 의 승계 복사본(승계 id 첫 bar)
은 복사하지 않는다(R2 링크가 옛 id 의 갭 사건을 적용하므로 이중 계상 방지).

CLI:
  ledger_build.py --root <rc1 root(로컬 복사본 권장)> --stage <stage> [--base-data <base_data.parquet>] [--out <parquet>]
  ledger_build.py --incremental --prev <이전 빌드> --asof <T−1> --out <stage> [...]   (D1 일별 증분, ledger_incremental.py)
  ledger_build.py --self-test
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
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
from db import rss_gb  # noqa: E402

SCRATCH = Path("/tmp/claude-1008/-home-jgo/3f2abe1d-a2a7-4017-9ac1-2c7cb961e311/scratchpad")
DEFAULT_ROOT = SCRATCH / "stage_rc2" / "R1" / "rc1_copy"
DEFAULT_BASE = SCRATCH / "stage_v21" / "T2" / "base_data.parquet"
DEFAULT_STAGE = SCRATCH / "stage_rc2" / "R1"
START = pd.Timestamp("2000-01-01")          # 원장 범위(base_data 2000-01-03 ~)
DAY = pd.Timedelta(days=1)
USD = 160
# ciqdividend 현금 타입: rc1/build_distributions 와 동일(핵심 1,4,5,7,8,10 + 원천징수 변형 6,15,17,18,19,23) + 33 Scrip(cashdividendflag=1).
# 16/24(캐나다 10% 변형)는 전부 1982 년 이전이라 범위 밖. 11/21/22 는 ciqdividend 에 없음(ciqsplit 타입), 20 = 스핀 후보, 31/32 = 권리.
CASH_TYPES = (1, 4, 5, 6, 7, 8, 10, 15, 17, 18, 19, 23, 33)
SPECIAL_SUPP = {"Special dividend", "Special dividend plus income", "Extra dividend", "Extra dividend plus income",
                "Liquidation", "Liquidation / Surrender of certificates required", "Return of capital",
                "Return of capital plus income", "Estimated return of capital", "Estimated return of capital plus income",
                "Final payment / Transfer books are closed", "Additional dividend", "Year end dividend",
                "Year end dividend plus income"}
SPLIT_TYPE_EVENT = {11: "stock_dividend", 21: "stock_dividend", 12: "split", 22: "split", 33: "split", 34: "split"}
EVENT_TYPES = ("cash_dividend", "special_dividend", "stock_dividend", "split", "spin_off", "rights", "warrant",
               "merger_cash", "merger_stock", "rename", "relist", "other")
LEG_TYPES = ("cash", "stock", "quantity", "rights")
VALUE_SOURCES = ("child_close_ex", "wi_close", "event_value", "vendor_implied", "unknown")
STATUSES = ("ok", "estimated", "unresolved")
SOURCE_PRIORITY = {"manual_override": 0, "lineage": 1, "distributions": 2, "ciqsplitcache": 2, "ciqsplit": 2,
                   "ciqdividend": 3, "co_acthist": 3, "corporate_actions": 4}   # 같은 키 충돌 시 작은 값이 남음
VALUE_TOL_ABS, VALUE_TOL_REL = 0.03, 0.15    # 벤더 implied ↔ 부모 가격 갭 교차확인 허용치(rc1 VALUE_TOL 0.03 + 상대 15%)
# R1c ②(V2 §4-2 ②): P10 ①/③ 값의 가격 정합 검사 — 사건 가치/전일 종가(checkFrac) ≥ 0.15 인데 부모 배당락 하락(dropFrac) < 0.05 이거나
# 함의 수익률(checkFrac − dropFrac) > 0.40 이면 불일치. 단일 leg ① 는 ±창 안에서 부모 하락과 자회사 종가가 맞는 날로 exDate 를 교정, 못 찾으면 unresolved.
IMPLAUSIBLE_CHECK_MIN, IMPLAUSIBLE_DROP_MAX, IMPLAUSIBLE_JUMP = 0.15, 0.05, 0.40
DATE_SEARCH_BEFORE_DAYS, DATE_SEARCH_AFTER_DAYS = 10, 45
JUMP_TOL = 0.25                              # 분할일 raw 점프 × parentFactor 가 1 에서 벗어나도 되는 폭(시장 변동)
CLOSES_MAX_DATE = None                       # D1 게이트 시뮬레이션(--closes-max-date): RawCloses 가 이 날짜 뒤의 종가를 읽지 않음(캐시도 쓰지 않음)

LEDGER_COLUMNS = ["eventId", "tradingItemId", "exDate", "eventType", "legType", "cashPerShare", "cashCurrency", "fxToUSD",
                  "parentFactor", "childTradingItemId", "childRatio", "childValueSource", "childValuePerOldShare",
                  "subscriptionPrice", "expiryDate", "exerciseRatio", "payDate", "recordDate", "announcedDate", "source",
                  "evidence", "status", "isEstimate", "repairOf", "notes", "keeperEventId", "exportExclude"]
LEDGER_SCHEMA = pa.schema([
    ("eventId", pa.string()), ("tradingItemId", pa.int64()), ("exDate", pa.timestamp("ns")), ("eventType", pa.string()),
    ("legType", pa.string()), ("cashPerShare", pa.float64()), ("cashCurrency", pa.string()), ("fxToUSD", pa.float64()),
    ("parentFactor", pa.float64()), ("childTradingItemId", pa.int64()), ("childRatio", pa.float64()),
    ("childValueSource", pa.string()), ("childValuePerOldShare", pa.float64()), ("subscriptionPrice", pa.float64()),
    ("expiryDate", pa.timestamp("ns")), ("exerciseRatio", pa.float64()), ("payDate", pa.timestamp("ns")),
    ("recordDate", pa.timestamp("ns")), ("announcedDate", pa.timestamp("ns")), ("source", pa.string()),
    ("evidence", pa.string()), ("status", pa.string()), ("isEstimate", pa.bool_()), ("repairOf", pa.string()),
    ("notes", pa.string()), ("keeperEventId", pa.string()), ("exportExclude", pa.bool_()),
])
REPAIR_COLUMNS = ["tradingItemId", "exDate", "field", "before", "after", "evidence", "source"]

log = logging.getLogger("cache_v21.ledger_build")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 22), b""):
            h.update(block)
    return h.hexdigest()


def _f(v, digits=6) -> str:
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "NaN"
    return f"{float(v):.{digits}g}"


def _d(v) -> str:
    return "NaT" if pd.isna(v) else str(pd.Timestamp(v).date())


# --------------------------------------------------------------------------------------
# 거래일 캘린더·raw 종가 lookup
# --------------------------------------------------------------------------------------
class Calendar:
    """calendar.parquet(isTradingDay) 기반. 범위 밖 날짜는 주말만 다음 월요일로 옮긴다(공휴일 정보 없음)."""

    def __init__(self, days: np.ndarray):
        self.days = np.asarray(days, dtype="datetime64[D]")
        self.first, self.last = self.days[0], self.days[-1]

    @classmethod
    def from_root(cls, root: Path) -> "Calendar":
        c = pd.read_parquet(root / "calendar.parquet")
        return cls(c.loc[c.isTradingDay, "date"].to_numpy().astype("datetime64[D]"))

    def align(self, dates) -> tuple[np.ndarray, np.ndarray]:
        """(aligned datetime64[ns], moved bool). 캘린더 안이면 다음 거래일(같은 날 포함), 밖이면 주말 → 월요일."""
        d = np.asarray(pd.to_datetime(dates)).astype("datetime64[D]")
        out = d.copy()
        inside = (d >= self.first) & (d <= self.last) & ~np.isnat(d)
        pos = np.searchsorted(self.days, d[inside], side="left")
        out[inside] = self.days[np.minimum(pos, len(self.days) - 1)]
        outside = ~inside & ~np.isnat(d)
        dow = (d[outside].astype("int64") + 3) % 7      # 1970-01-01 = 목요일(3) → 0=월 … 6=일
        shift = np.where(dow == 5, 2, np.where(dow == 6, 1, 0))
        out[outside] = d[outside] + shift.astype("timedelta64[D]")
        moved = out != d
        return out.astype("datetime64[ns]"), moved

    def prev(self, dates) -> np.ndarray:
        """직전 거래일(캘린더 안에서만; 밖이면 NaT)."""
        d = np.asarray(pd.to_datetime(dates)).astype("datetime64[D]")
        pos = np.searchsorted(self.days, d, side="left") - 1
        ok = (pos >= 0) & (d <= self.last) & ~np.isnat(d)
        out = np.full(len(d), np.datetime64("NaT", "D"))
        out[ok] = self.days[pos[ok]]
        return out.astype("datetime64[ns]")


class RawCloses:
    """base_data.parquet 의 raw close 를 필요한 id 만 메모리에 올려 (id, date) 점 lookup·구간 조회를 한다.

    키 = id << 20 | (days since 1970 + 30000) (prices_factors.FactorLookup 과 같은 방식)."""

    OFFSET = 30000

    def __init__(self, path, ids=None, cache: Path | None = None):
        """path: base_data.parquet 또는 [base_data, aux_base_data, ...](R3 보조 종목 가격을 합쳐 자회사 평가에 씀)."""
        t0 = time.time()
        paths = [Path(x) for x in (path if isinstance(path, (list, tuple)) else [path]) if x is not None and Path(x).exists()]
        if CLOSES_MAX_DATE is not None:
            cache = None                                   # 날짜 컷은 캐시와 섞지 않는다(항상 base_data 에서 읽고 컷)
        if cache is not None and cache.exists():
            t = pq.read_table(cache)
            need = set(int(i) for i in ids) if ids is not None else None
            have = set(t.column("tradingItemId").unique().to_pylist())
            if need is not None and not need <= have:
                t = None
        else:
            t = None
        if t is None:
            parts = []
            for pth in paths:
                tt = pq.read_table(pth, columns=["tradingItemId", "date", "close"])
                if ids is not None:
                    keep = pa.compute.is_in(tt.column("tradingItemId"), value_set=pa.array(sorted(set(int(i) for i in ids)), pa.int64()))
                    tt = tt.filter(keep)
                close = tt.column("close").to_numpy(zero_copy_only=False).astype(np.float64)
                tt = tt.filter(pa.array(np.isfinite(close)))
                if CLOSES_MAX_DATE is not None:
                    d_i8 = tt.column("date").cast(pa.timestamp("ns")).to_numpy(zero_copy_only=False).astype("datetime64[ns]")
                    tt = tt.filter(pa.array(d_i8 <= np.datetime64(pd.Timestamp(CLOSES_MAX_DATE), "ns")))
                parts.append(tt.cast(pa.schema([("tradingItemId", pa.int64()), ("date", pa.timestamp("ns")), ("close", pa.float32())])))
            t = pa.concat_tables(parts) if parts else pa.table({"tradingItemId": pa.array([], pa.int64()), "date": pa.array([], pa.timestamp("ns")), "close": pa.array([], pa.float32())})
            if cache is not None:
                cache.parent.mkdir(parents=True, exist_ok=True)
                pq.write_table(t, cache, compression="zstd")
        self.ids = t.column("tradingItemId").to_numpy().astype(np.int64)
        self.days = t.column("date").to_numpy().astype("datetime64[D]").astype(np.int64)
        self.close = t.column("close").to_numpy(zero_copy_only=False).astype(np.float64)
        order = np.lexsort((self.days, self.ids))
        self.ids, self.days, self.close = self.ids[order], self.days[order], self.close[order]
        self.key = (self.ids << 20) | (self.days + self.OFFSET)
        if len(self.key) > 1 and not (np.diff(self.key) > 0).all():
            keep = np.r_[True, np.diff(self.key) > 0]              # base ∪ aux 가 같은 (id, date) 를 갖는 경우 앞(base) 우선
            self.ids, self.days, self.close, self.key = self.ids[keep], self.days[keep], self.close[keep], self.key[keep]
        self.id_set = set(np.unique(self.ids).tolist())
        log.info("RawCloses: %d rows, %d ids, %.1fs", len(self.key), len(self.id_set), time.time() - t0)

    def _q(self, ids, dates):
        ids = np.asarray(ids, dtype=np.int64)
        d = np.asarray(pd.to_datetime(dates)).astype("datetime64[D]").astype(np.int64)
        return ids, d, (ids << 20) | (d + self.OFFSET)

    def at(self, ids, dates) -> np.ndarray:
        """그 날 raw close(없으면 NaN)."""
        ids, d, q = self._q(ids, dates)
        pos = np.searchsorted(self.key, q)
        posc = np.minimum(pos, len(self.key) - 1)
        hit = (self.key[posc] == q) & ~np.isnat(np.asarray(pd.to_datetime(dates)))
        out = np.full(len(q), np.nan)
        out[hit] = self.close[posc[hit]]
        return out

    def before(self, ids, dates) -> tuple[np.ndarray, np.ndarray]:
        """exDate 직전 관측 종가와 그 날짜(같은 id 안에서; 없으면 NaN/NaT)."""
        ids, d, q = self._q(ids, dates)
        pos = np.searchsorted(self.key, q, side="left") - 1
        ok = pos >= 0
        posc = np.maximum(pos, 0)
        ok &= self.ids[posc] == ids
        out = np.full(len(q), np.nan)
        dd = np.full(len(q), np.datetime64("NaT", "D"))
        out[ok] = self.close[posc[ok]]
        dd[ok] = self.days[posc[ok]].astype("datetime64[D]")
        return out, dd.astype("datetime64[ns]")

    def on_or_after(self, ids, dates, max_days: int = 7) -> tuple[np.ndarray, np.ndarray]:
        """exDate 이후 첫 관측 종가(max_days 이내)와 날짜. P10 위반 방지를 위해 at() 와 구분해 진단용으로만 쓴다."""
        ids, d, q = self._q(ids, dates)
        pos = np.searchsorted(self.key, q, side="left")
        posc = np.minimum(pos, len(self.key) - 1)
        ok = (self.ids[posc] == ids) & (self.days[posc] - d <= max_days) & (pos < len(self.key))
        out = np.full(len(q), np.nan)
        dd = np.full(len(q), np.datetime64("NaT", "D"))
        out[ok] = self.close[posc[ok]]
        dd[ok] = self.days[posc[ok]].astype("datetime64[D]")
        return out, dd.astype("datetime64[ns]")

    def series(self, tid: int, d0, d1) -> pd.Series:
        """[d0, d1] 구간의 raw close(index=date)."""
        lo = (int(tid) << 20) | (np.datetime64(pd.Timestamp(d0), "D").astype(np.int64) + self.OFFSET)
        hi = (int(tid) << 20) | (np.datetime64(pd.Timestamp(d1), "D").astype(np.int64) + self.OFFSET)
        a, b = np.searchsorted(self.key, lo, side="left"), np.searchsorted(self.key, hi, side="right")
        return pd.Series(self.close[a:b], index=pd.DatetimeIndex(self.days[a:b].astype("datetime64[D]")))


# --------------------------------------------------------------------------------------
# 입력
# --------------------------------------------------------------------------------------
def load_inputs(root: Path, work: Path, lineage_path=None, lifecycle_path=None) -> dict:
    """rc1 발행본(또는 로컬 복사본) + work 의 DB 추출물(ciqsplit, ciqsplitcache, ciqdividendcache 비현금, ciqcurrency).
    lineage_path / lifecycle_path 가 있으면 rc1 master 대신 그 파일(R3 lineage v2·lifecycle 패치본)을 쓴다."""
    root = Path(root)
    inp = {
        "dividends": pd.read_parquet(root / "dividends" / "dividends_long.parquet"),
        "distributions": pd.read_parquet(root / "distributions" / "distributions.parquet"),
        "split": pd.read_parquet(root / "factors" / "split.parquet"),
        "dividend_factor": pd.read_parquet(root / "factors" / "dividend.parquet"),
        "events": pd.read_parquet(root / "events" / "corporate_actions.parquet"),
        "lineage": pd.read_parquet(lineage_path if lineage_path and Path(lineage_path).exists() else root / "master" / "lineage.parquet"),
        "lifecycle": pd.read_parquet(lifecycle_path if lifecycle_path and Path(lifecycle_path).exists() else root / "master" / "lifecycle.parquet"),
        "instruments": pd.read_parquet(root / "master" / "instruments.parquet"),
        "current": pd.read_parquet(root / "master" / "current.parquet", columns=["tradingItemId", "symbol"]),
        "membership": pd.read_parquet(root / "universe" / "membership.parquet"),
    }
    for name in ("ciqsplit", "ciqsplitcache", "divcache_noncash", "ciqcurrency"):
        p = Path(work) / f"work_{name}.parquet"
        inp[name] = pd.read_parquet(p) if p.exists() else None
    if lifecycle_path and Path(lifecycle_path).exists():
        aux_lc = Path(lifecycle_path).with_name("lifecycle_aux.parquet")          # R3 보조 종목 lifecycle(있으면 귀속·앵커 판단에 합침)
        if aux_lc.exists():
            a = pd.read_parquet(aux_lc)
            inp["lifecycle"] = pd.concat([inp["lifecycle"], a[~a.tradingItemId.isin(inp["lifecycle"].tradingItemId)]], ignore_index=True)
    inp["lineage_source"] = str(lineage_path if lineage_path and Path(lineage_path).exists() else root / "master" / "lineage.parquet")
    inp["lifecycle_source"] = str(lifecycle_path if lifecycle_path and Path(lifecycle_path).exists() else root / "master" / "lifecycle.parquet")
    return inp


def pull_db(work: Path, ids, force: bool = False) -> None:
    """xfeed 읽기 전용 세션으로 작은 보조 테이블을 work 에 캐시(id 청크 ≤ 1000)."""
    from db import DB, chunked

    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    ids = sorted(int(i) for i in ids)
    todo = {
        "ciqsplit": ("select splitid, tradingitemid, exdate, paydate, recorddate, announceddate, rate, splittypeid, appliedflag "
                     "from xfl_target.ciqsplit where tradingitemid = any(%s)", True),
        "ciqsplitcache": ("select tradingitemid, cumulativesplitfactor, latestsplitfactor, splitdate, nextsplitdate "
                          "from xfl_target.ciqsplitcache where tradingitemid = any(%s)", True),
        "divcache_noncash": ("select tradingitemid, dividenddate, nextdividenddate, paydate, recorddate, divamount, currencyid, "
                             "adjustmentfactor, splitadjdivamount, dividendpaymenttypeid from xfl_target.ciqdividendcache "
                             "where tradingitemid = any(%s) and dividendpaymenttypeid <> 1", True),
        "ciqcurrency": ("select currencyid, isocode from xfl_target.ciqcurrency order by 1", False),
    }
    db = DB(application_name="cache_v21_R1_ledger")
    try:
        for name, (sql, chunked_ids) in todo.items():
            p = work / f"work_{name}.parquet"
            if p.exists() and not force:
                continue
            t0 = time.time()
            if chunked_ids:
                df = pd.concat([db.query(sql, (c,)) for c in chunked(ids, 1000)], ignore_index=True)
            else:
                df = db.query(sql)
            for c in df.columns:
                if df[c].dtype == object and c.endswith("date"):
                    df[c] = pd.to_datetime(df[c])
                elif str(df[c].dtype) == "object" and c in ("rate", "cumulativesplitfactor", "latestsplitfactor", "divamount",
                                                            "adjustmentfactor", "splitadjdivamount"):
                    df[c] = df[c].astype(float)
            df.to_parquet(p, index=False)
            log.info("pulled %s: %d rows %.1fs", name, len(df), time.time() - t0)
    finally:
        db.close()


# --------------------------------------------------------------------------------------
# leg 생성
# --------------------------------------------------------------------------------------
def _empty_rows() -> dict:
    return {c: [] for c in LEDGER_COLUMNS if c != "eventId"}


def _row(**kw) -> dict:
    r = {c: None for c in LEDGER_COLUMNS if c != "eventId"}
    r.update({"cashPerShare": np.nan, "fxToUSD": np.nan, "parentFactor": 1.0, "childRatio": np.nan,
              "childValueSource": "unknown", "childValuePerOldShare": np.nan, "subscriptionPrice": np.nan,
              "exerciseRatio": np.nan, "expiryDate": pd.NaT, "payDate": pd.NaT, "recordDate": pd.NaT,
              "announcedDate": pd.NaT, "status": "ok", "isEstimate": False, "repairOf": None, "notes": "",
              "cashCurrency": None, "childTradingItemId": None, "evidence": "", "keeperEventId": None, "exportExclude": False})
    r.update(kw)
    return r


def build_cash_rows(dv: pd.DataFrame, cal: Calendar, ccy: dict, gross_first) -> tuple[pd.DataFrame, list]:
    """ciqdividend 현금 타입 → (id, exDate) 당 1 행(합산). gross_first(df) 는 ledger_repairs.gross_first(세전 규칙, 교정 로그)."""
    c = dv[(dv.exDate >= START) & dv.dividendTypeId.isin(CASH_TYPES) & dv.divAmount.notna() & (dv.divAmount > 0)].copy()
    c, repairs = gross_first(c)                       # 컬럼 amountLocal, amountNote 추가
    ex_al, moved = cal.align(c.exDate.to_numpy())
    c["exDateAligned"] = ex_al
    c["moved"] = moved
    c["fxToUSD"] = np.where(c.currencyId.fillna(USD).astype(int) == USD, 1.0, 1.0 / c.fxRate.astype(float))
    c["amountUSD"] = c.amountLocal * c.fxToUSD
    c["special"] = (c.dividendTypeId == 4) | c.supplementalTypeName.isin(SPECIAL_SUPP)
    c["ccy"] = c.currencyId.fillna(USD).astype(int).map(lambda k: ccy.get(int(k), f"ccy{int(k)}"))
    c["divIdStr"] = c.dividendId.astype(int).astype(str)
    c["typeStr"] = c.dividendTypeId.astype(int).astype(str)
    c["movedFrom"] = np.where(c.moved, c.exDate.dt.strftime("%Y-%m-%d"), "")
    c["repairOf"] = c.repairOf.fillna("")
    c["amountNote"] = c.amountNote.fillna("")
    cnt = c.groupby(["tradingItemId", "exDateAligned"]).dividendId.transform("size")
    one = c[cnt == 1]
    agg1 = pd.DataFrame({"tradingItemId": one.tradingItemId, "exDateAligned": one.exDateAligned, "n": 1, "amt": one.amountUSD,
                         "nan_fx": one.amountUSD.isna(), "special": one.special, "ccy_n": 1, "ccy": one.ccy, "fx": one.fxToUSD,
                         "payDate": one.payDate, "recordDate": one.recordDate, "announcedDate": one.announcedDate, "ids": one.divIdStr,
                         "types": one.typeStr, "moved": one.movedFrom, "note": one.amountNote, "repairOf": one.repairOf})
    multi = c[cnt > 1]
    g = multi.groupby(["tradingItemId", "exDateAligned"], sort=True)
    aggn = g.agg(n=("dividendId", "size"), amt=("amountUSD", "sum"), nan_fx=("amountUSD", lambda x: bool(x.isna().any())),
                 special=("special", "any"), ccy_n=("ccy", "nunique"), ccy=("ccy", "first"), fx=("fxToUSD", "first"),
                 payDate=("payDate", "max"), recordDate=("recordDate", "max"), announcedDate=("announcedDate", "min"),
                 ids=("divIdStr", ",".join), types=("typeStr", lambda x: ",".join(sorted(set(x)))),
                 moved=("movedFrom", lambda x: ",".join(sorted({v for v in x if v}))),
                 note=("amountNote", lambda x: ";".join(sorted({v for v in x if v}))),
                 repairOf=("repairOf", lambda x: ";".join(sorted({v for v in x if v})))).reset_index()
    agg = pd.concat([agg1, aggn], ignore_index=True).sort_values(["tradingItemId", "exDateAligned"], kind="stable").reset_index(drop=True)
    notes = np.where(agg.n > 1, "merged_rows=" + agg.n.astype(str), "")
    notes = np.where(agg.moved != "", np.where(notes != "", notes + ";", "") + "exDate_moved_from=" + agg.moved, notes)
    notes = np.where(agg.nan_fx, np.where(notes != "", notes + ";", "") + "fx_missing", notes)
    notes = np.where(agg.note != "", np.where(notes != "", notes + ";", "") + agg.note, notes)
    out = pd.DataFrame({
        "tradingItemId": agg.tradingItemId.astype(np.int64), "exDate": pd.to_datetime(agg.exDateAligned),
        "eventType": np.where(agg.special, "special_dividend", "cash_dividend"), "legType": "cash",
        "cashPerShare": np.where(agg.nan_fx, np.nan, agg.amt), "cashCurrency": np.where(agg.ccy_n == 1, agg.ccy, "mixed"),
        "fxToUSD": np.where(agg.ccy_n == 1, agg.fx, np.nan), "parentFactor": 1.0, "childTradingItemId": None, "childRatio": np.nan,
        "childValueSource": "unknown", "childValuePerOldShare": np.nan, "subscriptionPrice": np.nan, "expiryDate": pd.NaT,
        "exerciseRatio": np.nan, "payDate": agg.payDate, "recordDate": agg.recordDate, "announcedDate": agg.announcedDate,
        "source": "ciqdividend", "evidence": "dividendId=" + agg.ids + ";types=" + agg.types,
        "status": np.where(agg.nan_fx, "unresolved", "ok"), "isEstimate": False,
        "repairOf": agg.repairOf.where(agg.repairOf != "", None), "notes": notes,
    })
    log.info("cash rows: %d (from %d ciqdividend rows), repairs %d", len(out), len(c), len(repairs))
    return out, repairs


def split_boundaries(split: pd.DataFrame) -> pd.DataFrame:
    """factors/split 구간 경계(계수가 바뀌는 validFrom ≥ START) → (tradingItemId, validFrom, factorPrev, factorNext, sourcePrev, sourceNext)."""
    s = split.sort_values(["tradingItemId", "validFrom"]).reset_index(drop=True)
    same = s.tradingItemId.to_numpy()[1:] == s.tradingItemId.to_numpy()[:-1]
    fp, fn = s.factor.to_numpy()[:-1], s.factor.to_numpy()[1:]
    chg = same & (fp != fn)
    idx = np.flatnonzero(chg) + 1
    b = pd.DataFrame({"tradingItemId": s.tradingItemId.to_numpy()[idx], "validFrom": s.validFrom.to_numpy()[idx],
                      "factorPrev": fp[idx - 1], "factorNext": fn[idx - 1], "sourcePrev": s.source.to_numpy()[idx - 1],
                      "sourceNext": s.source.to_numpy()[idx]})
    return b[b.validFrom >= START].reset_index(drop=True)


def build_split_rows(split: pd.DataFrame, ciqsplit: pd.DataFrame | None, cal: Calendar, closes: RawCloses) -> pd.DataFrame:
    """계수 경계 전수 → split/stock_dividend 행. parentFactor = factorPrev/factorNext. ciqsplit(±5일) 로 유형·증거,
    raw 종가 점프(close_ex/close_prev × parentFactor ≈ 1) 로 corroboration → status."""
    b = split_boundaries(split)
    b["parentFactor"] = b.factorPrev / b.factorNext
    ex_al, moved = cal.align(b.validFrom.to_numpy())
    b["exDate"] = ex_al
    b["moved"] = moved
    # ciqsplit 매칭(같은 id, |exdate − validFrom| ≤ 5일, 가장 가까운 것)
    b["splitId"] = np.nan
    b["splitType"] = np.nan
    b["splitRate"] = np.nan
    b["splitExDate"] = pd.NaT
    if ciqsplit is not None and len(ciqsplit):
        cs = ciqsplit.copy()
        cs["exdate"] = pd.to_datetime(cs.exdate)
        cs = cs[cs.exdate >= START - pd.Timedelta(days=10)]
        # D1: 같은 거리의 후보가 여럿이면(같은 ex-date 의 짝 분할 행) splitid 가 작은 행 — work 표의 행 순서와 무관하게 같은 결과
        by = {tid: g.sort_values("splitid", kind="stable") for tid, g in cs.groupby("tradingitemid")}
        sid, sty, srt, sex = [], [], [], []
        for tid, vf in zip(b.tradingItemId, b.validFrom):
            g = by.get(int(tid))
            if g is None:
                sid.append(np.nan); sty.append(np.nan); srt.append(np.nan); sex.append(pd.NaT)
                continue
            gap = (g.exdate - pd.Timestamp(vf)).abs().dt.days
            i = gap.idxmin()
            if gap[i] <= 5:
                sid.append(g.splitid[i]); sty.append(g.splittypeid[i]); srt.append(float(g.rate[i]) if pd.notna(g.rate[i]) else np.nan); sex.append(g.exdate[i])
            else:
                sid.append(np.nan); sty.append(np.nan); srt.append(np.nan); sex.append(pd.NaT)
        b["splitId"], b["splitType"], b["splitRate"], b["splitExDate"] = sid, sty, srt, pd.to_datetime(sex)
    # raw 점프 corroboration: exDate 종가, 없으면(그 날 무시세) 다음 관측 종가(≤10 일)로 대신 확인(F5: CELH·FERG 형)
    c_ex = closes.at(b.tradingItemId, b.exDate)
    c_prev, d_prev = closes.before(b.tradingItemId, b.exDate)
    c_next, d_next = closes.on_or_after(b.tradingItemId, b.exDate, 10)
    used_next = ~np.isfinite(c_ex) & np.isfinite(c_next)
    jump = np.where(used_next, c_next, c_ex) / c_prev * b.parentFactor.to_numpy()
    b["jumpCheck"] = jump
    rows = []
    repairs = []
    for r, j, un, dn in zip(b.itertuples(index=False), jump, used_next, d_next):
        has_split = pd.notna(r.splitId)
        rate_ok = has_split and np.isfinite(r.splitRate) and abs(r.splitRate / r.parentFactor - 1.0) <= 1e-6
        jump_ok = np.isfinite(j) and abs(j - 1.0) <= JUMP_TOL
        et = SPLIT_TYPE_EVENT.get(int(r.splitType), "split") if has_split else "split"
        notes = [f"factor {_f(r.factorPrev)}->{_f(r.factorNext)}", f"src={r.sourceNext}"]
        if r.moved:
            notes.append(f"exDate_moved_from={_d(r.validFrom)}")
        if has_split:
            notes.append(f"ciqsplit rate={_f(r.splitRate)} type={int(r.splitType)} ex={_d(r.splitExDate)}" + ("" if rate_ok else " RATE_MISMATCH"))
        else:
            notes.append("no_ciqsplit_within_5d")
        notes.append((f"raw_jump_x_pf={_f(j, 4)}" + (f"(via_next_close@{_d(dn)})" if un else "")) if np.isfinite(j) else "raw_jump=NA")
        if jump_ok:
            status = "ok"
        elif not np.isfinite(j):
            status = "ok" if rate_ok else "estimated"      # 가격 없음(범위 밖·상장 전, 다음 10 일도 없음): ciqsplit 만으로 확인
            notes.append("no_price_around_exDate")
        elif rate_ok:
            status = "estimated"                            # 벤더 분할비는 맞으나 raw 가격이 비율대로 안 움직임(효력일·가격 의심)
            notes.append("raw_jump_mismatch")
        else:
            status = "unresolved"                           # ciqsplit 없이 계수만 움직임 + raw 불일치 = 인공물 후보
            notes.append("factor_step_without_price_move")
        csv_tag = re.search(r"rc2f(_est)?:([A-Za-z0-9_.-]+)", str(r.sourceNext))
        csv_repair = None
        if csv_tag:
            # L1 최종(리드 결정): CSV split add/set_factor/move_date 로 생긴/옮겨진 경계의 원장 행은 CSV 의 isEstimate 가 status 를 정한다 —
            # false → ok, true → estimated. 빌더의 가격 분류(raw_jump_mismatch / factor_step_without_price_move / RATE_MISMATCH) 는 notes 에 남기고
            # (applied_unverified 는 apply 로그 신호), repairOf 에 csv_override:<id> 태그
            csv_repair = f"csv_override:{csv_tag.group(2)}"
            notes.append(f"csv_split_boundary(builder_status={status})")
            if csv_tag.group(1):
                status = "estimated"
                notes.append("rc2f_override_estimate")
            else:
                status = "ok"
        if un and not jump_ok and np.isfinite(j):
            repairs.append({"tradingItemId": int(r.tradingItemId), "exDate": pd.Timestamp(r.validFrom), "field": "split.factor",
                            "before": f"{_f(r.factorPrev)}->{_f(r.factorNext)} @validFrom {_d(r.validFrom)}", "after": "check(no quote on exDate)",
                            "evidence": f"no close on exDate; next close {_f(c_next[len(rows)])}@{_d(dn)} vs prev {_f(c_prev[len(rows)])} x parentFactor {_f(r.parentFactor)} = {_f(j, 4)} (expected ~1); "
                                        + (f"ciqsplit rate={_f(r.splitRate)} type={int(r.splitType)}" if has_split else "no ciqsplit"),
                            "source": "factors/split"})
        src_base = str(r.sourceNext).split("|")[0]        # rc2f: '|rc2f:<id>' / '|rc2f_est:<id>' / '|rc2f_scaled:<id>' tags do not change the row's source
        if "+" in src_base and all(p == "ciqsplitcache" for p in src_base.split("+")):
            src_base = "ciqsplitcache"                     # merged (drop) interval of two cache intervals is still a cache interval
        rows.append(_row(tradingItemId=int(r.tradingItemId), exDate=pd.Timestamp(r.exDate), eventType=et, legType="quantity",
                         parentFactor=float(r.parentFactor), payDate=pd.NaT, source="ciqsplitcache" if src_base == "ciqsplitcache" else "ciqsplit" if has_split else "ciqsplitcache",
                         evidence=(f"split.validFrom={_d(r.validFrom)};splitId={int(r.splitId)}" if has_split else f"split.validFrom={_d(r.validFrom)}"),
                         status=status, isEstimate=(status == "estimated"), repairOf=csv_repair, notes=";".join(notes)))
    out = pd.DataFrame(rows)
    out.attrs["repairs"] = repairs
    log.info("split rows: %d (status %s; no-quote exDate checked via next close %d, mismatched %d)", len(out),
             out.status.value_counts().to_dict() if len(out) else {}, int(used_next.sum()), len(repairs))
    return out


def _active_id(child: int, ex: pd.Timestamp, lifecycle: pd.DataFrame, lineage: pd.DataFrame) -> tuple[int, str]:
    """exDate 에 실제 거래되던 id 로 치환(ciq 가 새 id 에 과거를 이어붙인 경우 rename/relist 옛 id). 반환 (id, note)."""
    lc = lifecycle
    if child not in lc.index:
        return child, ""
    ft, lt = lc.at[child, "firstTradeDate"], lc.at[child, "lastTradeDate"]
    if pd.notna(ft) and ex >= ft and (pd.isna(lt) or ex <= lt):
        return child, ""
    seen, cur = set(), child
    for _ in range(5):
        prev = lineage[(lineage.newId == cur) & lineage.relation.isin(["rename", "relist"])]
        if prev.empty or cur in seen:
            break
        seen.add(cur)
        cur = int(prev.oldId.iloc[0])
        if cur in lc.index:
            ft, lt = lc.at[cur, "firstTradeDate"], lc.at[cur, "lastTradeDate"]
            if pd.notna(ft) and ex >= ft and (pd.isna(lt) or ex <= lt):
                return cur, f"child_id_lineage:{child}->{cur}"
    return child, "child_not_tradable_on_exDate"


def build_stock_rows(dist: pd.DataFrame, dv: pd.DataFrame, cal: Calendar, lifecycle: pd.DataFrame, lineage: pd.DataFrame,
                     split_rows: pd.DataFrame | None = None, r3_rows: pd.DataFrame | None = None) -> pd.DataFrame:
    """distributions(kind=stock) ∪ ciqdividend type 20(distributions 에 없는 것) ∪ type 31(rights) ∪ R3 인계 → stock/rights 행(평가는 value_children).

    rc2 재작업(F2): (b) 벤더가 같은 사건을 날짜만 바꿔 반복 기재한 행(IAC 2008-08-21/22/23/24, Lumina 2005-05-19/20/21)은 부모별
    10 일 창에서 같은 (child, ratio) 또는 같은 원문 텍스트를 1 사건으로 합친다(anchor = ciqdividend type-20 날짜, 없으면 최초일);
    (c) 부모 자기 전환 leg(rc1 newIdMethod dup_of_leg, child 가 그 날 lineage 승계 id, child 없이 ratio 가 같은 날 rename 비율·분할계수와 같음)
    는 자회사가 아니므로 버린다(IAC→MTCH 2020-07-01 2.1584, VIA.B→CBS 0.5). 버린 행은 attrs['dropped'] 에 남긴다."""
    st = dist[(dist.kind == "stock") & (dist.exDate >= START)].copy()
    lc = lifecycle.set_index("tradingItemId")
    rows = []
    seen = set()
    for r in st.itertuples(index=False):
        ex, moved = cal.align([r.exDate])
        ex = pd.Timestamp(ex[0])
        child = int(r.newTradingItemId) if pd.notna(r.newTradingItemId) else None
        note_id = ""
        if child is not None:
            child, note_id = _active_id(child, ex, lc, lineage)
        ratio = float(r.ratioNewPerOld) if pd.notna(r.ratioNewPerOld) else np.nan
        ec = str(r.eventClass) if pd.notna(r.eventClass) else "unknown"
        et = {"spinoff": "spin_off", "merger": "merger_stock", "name_change": "other", "security_end": "other"}.get(ec, "spin_off")
        notes = [f"eventClass={ec}", f"rc1status={r.status}", f"ratioBasis={r.ratioBasis}"]
        if moved[0]:
            notes.append(f"exDate_moved_from={_d(r.exDate)}")
        if note_id:
            notes.append(note_id)
        if pd.notna(r.newIdMethod):
            notes.append(f"idMethod={r.newIdMethod}")
        if pd.notna(r.mismatchClass):
            notes.append(f"rc1mismatch={r.mismatchClass}")
        src = "distributions" if str(r.source) in ("ciqdividend20", "co_acthist") or str(r.source).startswith("web") else str(r.source)
        ev = f"distributions[{r.source}] leg={int(r.leg)}"
        if pd.notna(r.evidence) and str(r.evidence):
            ev += " | " + str(r.evidence)[:300]
        seen.add((int(r.tradingItemId), ex))
        rows.append(_row(tradingItemId=int(r.tradingItemId), exDate=ex, eventType=et, legType="stock", childTradingItemId=child,
                         childRatio=ratio, payDate=pd.NaT, recordDate=pd.NaT, source=src, evidence=ev, notes=";".join(notes),
                         **{"_cashEquiv": float(r.cashEquivalentText) if pd.notna(r.cashEquivalentText) else np.nan,
                            "_impliedFrac": float(r.impliedValueFracExCash) if pd.notna(r.impliedValueFracExCash) else np.nan,
                            "_leg": int(r.leg), "_text": str(r.evidence)[:300] if pd.notna(r.evidence) else "",
                            "_dup_of_leg": bool(pd.notna(r.newIdMethod) and "dup_of_leg" in str(r.newIdMethod)),
                            "_ciqdiv20": str(r.source) == "ciqdividend20"}))
    # ciqdividend type 20 rows not covered by distributions, and type 31 rights
    t20 = dv[(dv.exDate >= START) & dv.dividendTypeId.isin([20, 31])]
    for r in t20.itertuples(index=False):
        ex, moved = cal.align([r.exDate])
        ex = pd.Timestamp(ex[0])
        if int(r.dividendTypeId) == 20 and (int(r.tradingItemId), ex) in seen:
            continue
        et = "rights" if int(r.dividendTypeId) == 31 else "spin_off"
        rows.append(_row(tradingItemId=int(r.tradingItemId), exDate=ex, eventType=et, legType="rights" if et == "rights" else "stock",
                         childTradingItemId=None, childRatio=np.nan, payDate=r.payDate, recordDate=r.recordDate,
                         announcedDate=r.announcedDate, source="ciqdividend",
                         evidence=f"dividendId={int(r.dividendId)};type={int(r.dividendTypeId)}({r.dividendTypeName})",
                         notes="ciqdividend_only;no_terms" + (f";exDate_moved_from={_d(r.exDate)}" if moved[0] else ""),
                         **{"_cashEquiv": np.nan, "_impliedFrac": np.nan, "_leg": 0, "_text": "", "_dup_of_leg": False, "_ciqdiv20": True}))
    out = pd.DataFrame(rows)
    dropped = []
    n_dist = len(st)
    # (b) 벤더 반복 기재 병합(부모별 10 일 창)
    out = out.sort_values(["tradingItemId", "exDate", "_leg"], kind="stable").reset_index(drop=True)
    out["_ck"] = out.childTradingItemId.map(lambda x: -1 if x is None or pd.isna(x) else int(x))
    out["_rk"] = out.childRatio.round(8)
    keep = np.ones(len(out), dtype=bool)
    for tid, g in out.groupby("tradingItemId", sort=False):
        if len(g) < 2:
            continue
        g = g.sort_values(["exDate", "_leg"], kind="stable")
        clusters, cur = [], [g.index[0]]
        for i in g.index[1:]:
            c0 = out.loc[cur]
            same_text = bool(out.at[i, "_text"]) and (c0["_text"] == out.at[i, "_text"]).any()
            same_leg = ((c0["_ck"] == out.at[i, "_ck"]) & ((c0["_rk"] == out.at[i, "_rk"]) | (c0["_rk"].isna() & pd.isna(out.at[i, "_rk"])))).any()
            if (out.at[i, "exDate"] - c0.exDate.min()).days <= 10 and (same_text or same_leg):
                cur.append(i)
            else:
                clusters.append(cur)
                cur = [i]
        clusters.append(cur)
        for cl in clusters:
            if len(cl) < 2 or out.loc[cl, "exDate"].nunique() < 2:
                continue
            c = out.loc[cl]
            anchor_rows = c[c._ciqdiv20]
            anchor = anchor_rows.exDate.min() if len(anchor_rows) else c.exDate.min()
            dates = ",".join(sorted({_d(x) for x in c.exDate}))
            seen_legs = set()
            for i in cl:
                lk = (out.at[i, "_ck"], out.at[i, "_rk"] if pd.notna(out.at[i, "_rk"]) else None)
                if lk in seen_legs:
                    keep[i] = False
                    dropped.append({"tradingItemId": int(tid), "exDate": out.at[i, "exDate"], "eventType": out.at[i, "eventType"], "legType": "stock",
                                    "childKey": out.at[i, "_ck"], "cashPerShare": np.nan, "childRatio": out.at[i, "childRatio"], "source": out.at[i, "source"],
                                    "status": "", "reason": f"vendor_repeat_of_{_d(anchor)}", "notes": out.at[i, "notes"], "evidence": out.at[i, "evidence"]})
                    continue
                seen_legs.add(lk)
                if out.at[i, "exDate"] != anchor:
                    out.at[i, "notes"] = str(out.at[i, "notes"]) + f";exDate_aligned_from={_d(out.at[i, 'exDate'])}"
                    out.at[i, "exDate"] = anchor
                out.at[i, "notes"] = str(out.at[i, "notes"]) + f";vendor_repeat_dates={dates}"
    n_repeat = int((~keep).sum())
    out = out[keep].reset_index(drop=True)
    # (c) 부모 자기 전환 leg 제거
    succ = {}
    last_map = lc.lastTradeDate.to_dict()
    for r in lineage[lineage.relation.isin(["rename", "relist"])].itertuples(index=False):
        last = last_map.get(int(r.oldId), pd.NaT)
        if pd.notna(last) and (pd.Timestamp(last) - pd.Timestamp(r.effectiveDate)).days > 3:
            continue                                   # 옛 id 가 전환 뒤에도 거래됨(MCO→DNB, MEE→FLR): 승계가 아니라 진짜 분사 자회사
        succ.setdefault(int(r.oldId), []).append((pd.Timestamp(r.effectiveDate), int(r.newId), float(r.ratio) if pd.notna(r.ratio) else np.nan))
    pf_same = {}
    if split_rows is not None and len(split_rows):
        pf_same = {(int(a), pd.Timestamp(b)): float(c) for a, b, c in zip(split_rows.tradingItemId, split_rows.exDate, split_rows.parentFactor)}
    self_conv = np.zeros(len(out), dtype=bool)
    why = {}
    a_tid, a_ex, a_ck, a_ratio, a_dup = out.tradingItemId.to_numpy(), out.exDate.to_numpy(), out["_ck"].to_numpy(), out.childRatio.to_numpy(dtype=float), out["_dup_of_leg"].to_numpy()
    for i in range(len(out)):
        reason = ""
        if a_dup[i]:
            reason = "rc1_dup_of_leg"
        ex_i = pd.Timestamp(a_ex[i])
        for eff, new_id, ratio in succ.get(int(a_tid[i]), []):
            if abs((eff - ex_i).days) <= 3:
                if a_ck[i] == new_id:
                    reason = reason or f"child_is_lineage_successor:{new_id}"
                elif a_ck[i] < 0 and np.isfinite(a_ratio[i]) and np.isfinite(ratio) and abs(a_ratio[i] - ratio) <= 1e-6 * max(1.0, ratio):
                    reason = reason or f"ratio_equals_lineage_ratio:{new_id}"
        if not reason and a_ck[i] < 0 and np.isfinite(a_ratio[i]):
            pf = pf_same.get((int(a_tid[i]), ex_i))
            if pf is not None and pf != 1.0 and abs(a_ratio[i] - pf) <= 1e-6 * max(1.0, pf):
                reason = "ratio_equals_same_day_split_factor"
        if reason:
            self_conv[i] = True
            why[i] = reason
    for i in np.flatnonzero(self_conv):
        r = out.iloc[i]
        dropped.append({"tradingItemId": int(r.tradingItemId), "exDate": r.exDate, "eventType": r.eventType, "legType": "stock", "childKey": int(a_ck[i]),
                        "cashPerShare": np.nan, "childRatio": r.childRatio, "source": r.source, "status": "", "reason": "parent_self_conversion:" + why[i],
                        "notes": r.notes, "evidence": r.evidence})
    out = out[~self_conv].reset_index(drop=True)
    # R3 인계 행 추가: 같은 (id, exDate) 사건의 자회사 미확인(child null) 행은 R3 행이 대체
    n_r3 = 0
    if r3_rows is not None and len(r3_rows):
        r3s = r3_rows[r3_rows.legType.isin(["stock", "rights"])].copy()
        if len(r3s):
            r3_keys = set(zip(r3s.tradingItemId.astype(int), pd.to_datetime(r3s.exDate)))
            repl = out.childTradingItemId.isna() & [(int(a), pd.Timestamp(b)) in r3_keys for a, b in zip(out.tradingItemId, out.exDate)]
            for r in out[repl].itertuples(index=False):
                dropped.append({"tradingItemId": int(r.tradingItemId), "exDate": r.exDate, "eventType": r.eventType, "legType": "stock", "childKey": -1,
                                "cashPerShare": np.nan, "childRatio": r.childRatio, "source": r.source, "status": "", "reason": "replaced_by_R3_handover", "notes": r.notes, "evidence": r.evidence})
            out = out[~repl].reset_index(drop=True)
            for c in out.columns:
                if c not in r3s.columns:
                    r3s[c] = None if c in ("_text",) else (False if c in ("_dup_of_leg", "_ciqdiv20") else np.nan)
            r3s["_ck"] = r3s.childTradingItemId.map(lambda x: -1 if x is None or pd.isna(x) else int(x))
            r3s["_rk"] = r3s.childRatio.round(8)
            out["_r3_status"] = None
            out = pd.concat([out, r3s[out.columns]], ignore_index=True)
            n_r3 = len(r3s)
    # 자회사 미확인 leg 가 한 사건에 여럿이면(키 충돌: child null) 한 행으로 합치고 leg 비율을 notes 에 남긴다(값은 사건 단위 평가)
    unk = out.childTradingItemId.isna() & (out.legType == "stock")
    grp = out[unk].groupby(["tradingItemId", "exDate"])
    drop_idx = []
    for (tid, ex), g in grp:
        if len(g) < 2:
            continue
        keep_i = g.index[0]
        out.at[keep_i, "childRatio"] = np.nan
        out.at[keep_i, "notes"] = str(out.at[keep_i, "notes"]) + f";unknown_child_legs={len(g)}:ratios=" + ",".join(_f(r, 6) for r in g.childRatio)
        drop_idx += list(g.index[1:])
    if drop_idx:
        out = out.drop(index=drop_idx).reset_index(drop=True)
    # 평가 전에 같은 (id, exDate, legType, child) 의 중복 leg 제거(finalize 와 같은 source 우선순위, R3 행 _prio 0.5 우선) — 다중 leg
    # 잔여 배분(leg_share)의 미시세 leg 수가 맞도록(R1b: SLM 2014-05-01 두 번째 leg 0.215 → 0.43)
    if "_prio" not in out.columns:
        out["_prio"] = np.nan
    out["_pk"] = np.where(out["_prio"].notna(), out["_prio"], out.source.map(lambda s: SOURCE_PRIORITY.get(str(s).split(":")[0], 5)))
    out = out.sort_values(["tradingItemId", "exDate", "legType", "_ck", "_pk"], kind="stable")
    n_before = len(out)
    dup_pre = out.duplicated(["tradingItemId", "exDate", "legType", "_ck"], keep="first")
    for i in np.flatnonzero(dup_pre.to_numpy()):          # itertuples 는 '_' 로 시작하는 컬럼명을 바꾸므로 iloc 사용
        r = out.iloc[i]
        dropped.append({"tradingItemId": int(r.tradingItemId), "exDate": r.exDate, "eventType": r.eventType, "legType": r.legType, "childKey": int(r["_ck"]),
                        "cashPerShare": np.nan, "childRatio": r.childRatio, "source": r.source, "status": "", "reason": "key_collision(lower_priority_source)",
                        "notes": r.notes, "evidence": r.evidence})
    out = out[~dup_pre].reset_index(drop=True)
    n_dedup = n_before - len(out)
    out = out.drop(columns=["_ck", "_rk", "_text", "_dup_of_leg", "_ciqdiv20", "_pk"])
    if "_r3_status" not in out.columns:
        out["_r3_status"] = None
    if n_dedup:
        log.info("stock rows: %d duplicate legs removed before valuation", n_dedup)
    out.attrs["dropped"] = dropped
    log.info("stock/rights rows: %d (distributions %d, ciqdividend-only %d, vendor repeats merged %d, self-conversion legs dropped %d, "
             "R3 rows %d, unknown-child legs merged %d)", len(out), n_dist, int(len(out[out.source == 'ciqdividend'])), n_repeat,
             int(self_conv.sum()), n_r3, len(drop_idx))
    return out


ENDED_MARKER_TAG = "candidate:ended_after_co_acthist_latest"


def read_source_status(path) -> dict:
    """work/source_status.json (D1 증분·prepare-full 이 씀): 원천별 최신 적재일. 없으면 {} (rc2f 재현 빌드는 마커 규칙 비활성)."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


MARKER_TERMS_TYPES = ("merger_cash", "merger_stock", "other", "spin_off", "rights", "warrant")
MARKER_TERMS_WINDOW_DAYS = 7          # a terms row of the same id dated within this many calendar days (~5 trading days) before lastTradeDate or later


def ended_without_terms_markers(lifecycle: pd.DataFrame, events: pd.DataFrame, lineage: pd.DataFrame, cal: Calendar, since, until,
                                ledger_rows: pd.DataFrame | None = None) -> pd.DataFrame:
    """D4 인계(2026-09-21): xfeed co_acthist 적재가 멈춘 뒤(since = co_acthist 최신 datadate) 종료된 id(lifecycle status confirmed, since <
    lastTradeDate ≤ until) 중 events 파일에 조건(현금/주식 leg)이 없고 lineage 옛 id(rename/relist 승계) 도 아닌 것 → **표시용** 행
    (eventType other, legType stock, child/ratio/값 없음, source lifecycle, status unresolved, notes candidate:ended_after_co_acthist_latest=…).
    엔진은 보유 시 unresolved_leg 로 감지한다(값 추정 없음; 엔진의 default_settlement 와 병행). provisional(data_gap) 은 종료 사건이 아니라 제외.
    전체 빌드·증분 모두 build() 안의 같은 단계에서 만든다(work/source_status.json 이 있을 때만).
    R-D1 P2-1: 원장 자체에 같은 id 의 조건 행(ledger_rows: merger/other/spin/rights/warrant, 어느 source 든 — manual_override·distributions·R3·CSV add)
    이 lastTradeDate − MARKER_TERMS_WINDOW_DAYS 이후에 있으면 조건이 있는 것이므로 마커를 만들지 않는다."""
    since, until = pd.Timestamp(since), pd.Timestamp(until)
    lc = lifecycle[lifecycle.lastTradeDate.notna()]
    if "status" in lc.columns:
        lc = lc[lc.status == "confirmed"]
    lc = lc[(lc.lastTradeDate > since) & (lc.lastTradeDate <= until)]
    if lc.empty:
        return pd.DataFrame()
    ev = events if events is not None else pd.DataFrame()
    legs = set(int(x) for x in ev[(ev.hasCashLeg.astype(bool) | ev.hasStockLeg.astype(bool))].tradingItemId) if len(ev) else set()
    code = {int(t): (None if pd.isna(c) else str(c)) for t, c in zip(ev.tradingItemId, ev.actionCode)} if len(ev) else {}
    old_ids = set(int(x) for x in lineage.oldId) if lineage is not None and len(lineage) and "oldId" in lineage.columns else set()
    terms = {}
    if ledger_rows is not None and len(ledger_rows):
        lr = ledger_rows[ledger_rows.eventType.isin(MARKER_TERMS_TYPES)]
        for t, d in zip(lr.tradingItemId, lr.exDate):
            if pd.notna(d):
                terms.setdefault(int(t), []).append(pd.Timestamp(d))
    rows = []
    n_terms_in_ledger = 0
    for r in lc.itertuples(index=False):
        tid = int(r.tradingItemId)
        if tid in legs or tid in old_ids:
            continue
        if any(d >= pd.Timestamp(r.lastTradeDate) - pd.Timedelta(days=MARKER_TERMS_WINDOW_DAYS) for d in terms.get(tid, ())):
            n_terms_in_ledger += 1
            continue
        ex, moved = cal.align([r.lastTradeDate])
        ex = pd.Timestamp(ex[0])
        reason = getattr(r, "endReason", None)
        rows.append(_row(tradingItemId=tid, exDate=ex, eventType="other", legType="stock", childTradingItemId=None, childRatio=np.nan, source="lifecycle",
                         evidence=f"lifecycle lastTradeDate={_d(r.lastTradeDate)} endReason={reason} status={getattr(r, 'status', None)}; events actionCode={code.get(tid)} (no cash/stock leg)",
                         status="unresolved", isEstimate=False,
                         notes=f"{ENDED_MARKER_TAG}={_d(since)};endReason={reason};no_terms" + (f";exDate_moved_from={_d(r.lastTradeDate)}" if moved[0] else "")))
    out = pd.DataFrame(rows)
    log.info("ended-without-terms markers (co_acthist latest %s < asOf %s): %d rows (ids with a terms row already in the ledger: %d)", _d(since), _d(until), len(out), n_terms_in_ledger)
    out.attrs["terms_in_ledger"] = n_terms_in_ledger
    return out


def build_merger_rows(ev: pd.DataFrame, lineage: pd.DataFrame, cal: Calendar) -> pd.DataFrame:
    """events/corporate_actions 의 현금·주식 leg → merger_cash / merger_stock(actionCode N/C 는 lineage rename 이 담당)."""
    e = ev[(ev.hasCashLeg | ev.hasStockLeg) & (ev.actionCode != "N/C")].copy()
    e = e[e.settlementDate >= START]
    rows = []
    for r in e.itertuples(index=False):
        ex, moved = cal.align([r.settlementDate])
        ex = pd.Timestamp(ex[0])
        base = dict(tradingItemId=int(r.tradingItemId), exDate=ex, source="corporate_actions",
                    notes=f"actionCode={r.actionCode};endDate={_d(r.endDate)};settlementDate={_d(r.settlementDate)};confidence={r.confidence};cacheUnitStatus={r.cacheUnitStatus}"
                          + (f";exDate_moved_from={_d(r.settlementDate)}" if moved[0] else ""))
        evd = f"events[{r.actionCode}] {str(r.actionText)[:200]}" if pd.notna(r.actionText) else f"events[{r.actionCode}] source={r.source}"
        if r.hasCashLeg:
            et = "merger_cash" if r.actionCode in ("ACQ", "MGD") else "other"
            ok = pd.notna(r.cashPerShare) and r.cashPerShare > 0
            rows.append(_row(**base, eventType=et, legType="cash", cashPerShare=float(r.cashPerShare) if ok else np.nan,
                             cashCurrency="USD", fxToUSD=1.0, payDate=pd.Timestamp(r.settlementDate), evidence=evd,
                             status="ok" if ok else "unresolved", isEstimate=False))
        if r.hasStockLeg:
            child = int(r.successorTradingItemId) if pd.notna(r.successorTradingItemId) else None
            ratio = float(r.successorRatio) if pd.notna(r.successorRatio) else np.nan
            et = "merger_stock" if r.actionCode in ("ACQ", "MGD") else "other"
            rows.append(_row(**base, eventType=et, legType="stock", childTradingItemId=child, childRatio=ratio,
                             payDate=pd.Timestamp(r.settlementDate), evidence=evd,
                             **{"_cashEquiv": np.nan, "_impliedFrac": np.nan, "_leg": 0}))
    out = pd.DataFrame(rows) if rows else pd.DataFrame(columns=[c for c in LEDGER_COLUMNS if c != "eventId"] + ["_cashEquiv", "_impliedFrac", "_leg"])
    log.info("merger rows: %d (cash %d, stock %d)", len(out), int((out.legType == "cash").sum()) if len(out) else 0,
             int((out.legType == "stock").sum()) if len(out) else 0)
    return out


def build_lineage_rows(lineage: pd.DataFrame, cal: Calendar, lifecycle: pd.DataFrame | None = None) -> pd.DataFrame:
    """rename/relist → quantity 행(parentFactor = ratio; NaN 은 1.0 가정·estimated). child = newId.
    lineage v2(R3) 의 ratioSource 가 assumed_1to1/stitched_1to1 이면 역시 estimated(근거 없는 1:1).
    옛 id 가 effectiveDate 뒤 3 일 넘게 계속 거래되면(MCO→DNB 같은 두 라인) 연결이 아니므로 unresolved 로 표시(R3 인계)."""
    ln = lineage[lineage.relation.isin(["rename", "relist"]) & (lineage.effectiveDate >= START)]
    has_src = "ratioSource" in ln.columns
    last_map = lifecycle.set_index("tradingItemId").lastTradeDate.to_dict() if lifecycle is not None else {}
    rows = []
    for r in ln.itertuples(index=False):
        ex, moved = cal.align([r.effectiveDate])
        ex = pd.Timestamp(ex[0])
        known = pd.notna(r.ratio) and r.ratio > 0
        src = str(getattr(r, "ratioSource", "") or "") if has_src else ""
        assumed = (not known) or src in ("assumed_1to1", "stitched_1to1") or src.startswith("rc2f_estimate")
        notes = [f"confidence={r.confidence}"]
        if has_src and src:
            notes.append(f"ratioSource={src}")
        if not known:
            notes.append("ratio_assumed_1.0")
        elif assumed:
            notes.append("ratio_assumed_by_R3(1:1)")
        if moved[0]:
            notes.append(f"exDate_moved_from={_d(r.effectiveDate)}")
        status = "estimated" if assumed else "ok"
        last_old = last_map.get(int(r.oldId), pd.NaT)
        if pd.notna(last_old) and (pd.Timestamp(last_old) - ex).days > 3:
            status = "unresolved"                      # 옛 id 가 전환 뒤에도 거래: 계보 연결이 아니라 두 라인(R3 재검토 대상)
            notes.append(f"old_id_continues_trading_until={_d(last_old)}(not_a_continuation)")
        rows.append(_row(tradingItemId=int(r.oldId), exDate=ex, eventType=str(r.relation), legType="quantity",
                         parentFactor=float(r.ratio) if known else 1.0, childTradingItemId=int(r.newId), childRatio=np.nan,
                         source="lineage", evidence=str(r.evidence)[:300], status=status,
                         isEstimate=assumed and status != "unresolved", notes=";".join(notes)))
    out = pd.DataFrame(rows) if rows else pd.DataFrame(columns=[c for c in LEDGER_COLUMNS if c != "eventId"])     # empty lineage keeps the contract columns (synthetic fixtures)
    log.info("lineage rows: %d (assumed ratio %d, old id continues trading %d)", len(out), int(out.isEstimate.sum()) if len(out) else 0,
             int((out.status == "unresolved").sum()) if len(out) else 0)
    return out


def vendor_gap_markers(candidates_path, dv: pd.DataFrame, cal: Calendar, lifecycle: pd.DataFrame, cash: pd.DataFrame | None = None,
                       split: pd.DataFrame | None = None, stock: pd.DataFrame | None = None, rc2_rules: bool = False, closes: "RawCloses | None" = None) -> pd.DataFrame:
    """R2 리뷰 §8-4(4) + 리드 규칙: 벤더 divadj 계수는 스텝을 밟는데 원장에 사건이 없는 셀. ciqdividend(전 타입, ±7 일) 에 원천이 있으면
    다른 leg 가 담당(표시 없음). 없으면
      · 타당성 검사 통과 → status=estimated, isEstimate=true, source/childValueSource=vendor_implied(P10 ④) 인 현금 행(cashPerShare = implied D)
        검사: 같은 종목의 인접 배당(±400 일, 당일 제외) ≥ 2 건, implied D 가 인접 중앙값의 0.5~2 배, 가장 가까운 인접 배당과 20 일 이상·
        인접 간격 중앙값의 1.5 배 이하, 같은 날 분할·스핀 행 없음
      · 실패 → status=unresolved, cashPerShare NaN, notes 에 implied D(P12: "자료 없음" 을 0 과 구분)."""
    if candidates_path is None or not Path(candidates_path).exists():
        return pd.DataFrame()
    c = pd.read_csv(candidates_path)
    idc = "tradingItemId" if "tradingItemId" in c.columns else "id"
    c["date"] = pd.to_datetime(c.date)
    # rc2f (X3 (b)): the ciqdividend check uses the cell's own interval (prevDate, date] (sparse names: PBHC 9-23 day gaps) when prevDate is known
    c["prevDate"] = pd.to_datetime(c.prevDate) if ("prevDate" in c.columns and not rc2_rules) else pd.NaT
    if not rc2_rules and closes is not None and c.prevDate.isna().any():
        # L1 fix 4: candidates without prevDate (older candidate lists) get the cell's own interval from raw closes — the vendor step of a sparse
        # name shows on the first print after the ex date (2637938: ciqdividend 01-14, step 01-23), which only the (prevDate, date] window sees
        m = c.prevDate.isna().to_numpy()
        _, pdts = closes.before(c[idc].to_numpy(np.int64)[m], c.date.to_numpy()[m])
        filled = pd.to_datetime(pdts)
        c.loc[m, "prevDate"] = filled
        c.loc[m & (c.prevDate.notna().to_numpy()), "prevDateSource"] = "raw_closes"
    dcol = next((k for k in ("vendorImpliedD", "vendor_implied_value", "impliedD") if k in c.columns), None)
    by = {tid: g for tid, g in dv[dv.exDate >= START - pd.Timedelta(days=30)].groupby("tradingItemId")}
    cash_by = {tid: g.sort_values("exDate") for tid, g in cash.groupby("tradingItemId")} if cash is not None and len(cash) else {}
    # 같은 종목의 다른 벤더 스텝 후보도 인접 배당으로 본다(ciqdividend 행이 통째로 없는 종목: 후보끼리 금액·주기가 맞으면 타당)
    cand_by = {}
    if dcol:
        for tid_, g in c.groupby(idc):
            cand_by[int(tid_)] = pd.DataFrame({"exDate": pd.to_datetime(g.date), "cashPerShare": g[dcol].astype(float)})
    same_day = set()
    for fr in (split, stock):
        if fr is not None and len(fr):
            same_day |= set(zip(fr.tradingItemId.astype(int), pd.to_datetime(fr.exDate)))
    rows = []
    n_src = n_est = n_unres = 0
    for r in c.itertuples(index=False):
        tid, date = int(getattr(r, idc)), pd.Timestamp(r.date)
        g = by.get(tid)
        pdt = pd.Timestamp(r.prevDate) if pd.notna(r.prevDate) else pd.NaT
        if g is None:
            near = None
        elif pd.notna(pdt):
            near = g[(g.exDate > pdt) & (g.exDate <= date + pd.Timedelta(days=7))]     # (prevDate, date+7]: vendor step may precede the ex date by a few days (R-X0 P2-7)
        else:
            near = g[(g.exDate - date).abs().dt.days <= 7]
        if near is not None and len(near):
            n_src += 1
            continue
        ex, _ = cal.align([date])
        ex = pd.Timestamp(ex[0])
        lg = cash_by.get(tid)                            # 재귀속된 실제 현금 행이 같은 (id, exDate) 에 있으면 마커를 만들지 않는다(R1b N1: 실제 행이 이김)
        if lg is not None and (lg.exDate == ex).any():
            n_src += 1
            continue
        implied = float(getattr(r, dcol)) if dcol and pd.notna(getattr(r, dcol)) else np.nan
        why = []
        parts = [x for x in (cash_by.get(tid), cand_by.get(tid)) if x is not None]
        adj = pd.concat([x[["exDate", "cashPerShare"]] for x in parts]).sort_values("exDate") if parts else None
        if adj is not None:
            adj = adj[(adj.exDate != ex) & (adj.exDate != date) & ((adj.exDate - ex).abs().dt.days <= 400) & adj.cashPerShare.notna()]
        if adj is None or len(adj) < 2:
            why.append("fewer_than_2_adjacent_dividends")
        else:
            med = float(adj.cashPerShare.median())
            if not (np.isfinite(implied) and 0.5 * med <= implied <= 2.0 * med):
                why.append(f"amount_off(median_adjacent={_f(med)})")
            gaps = (adj.exDate - ex).dt.days.abs()
            intervals = adj.exDate.diff().dt.days.dropna()
            med_int = float(intervals.median()) if len(intervals) else np.nan
            if gaps.min() < 20 or (np.isfinite(med_int) and gaps.min() > 1.5 * med_int):
                why.append(f"cadence_off(nearest={int(gaps.min())}d,median_interval={_f(med_int, 3)}d)")
        if (tid, ex) in same_day:
            why.append("split_or_spin_same_day")
        if not why and np.isfinite(implied) and implied > 0:
            n_est += 1
            rows.append(_row(tradingItemId=tid, exDate=ex, eventType="cash_dividend", legType="cash", cashPerShare=implied, cashCurrency="USD", fxToUSD=1.0,
                             source="vendor_implied", childValueSource="vendor_implied",
                             evidence=f"vendor divAdjFactor step on {date.date()} implies D={_f(implied)}; no ciqdividend row within ±7d; plausibility passed (P10 ④, P5 exception)",
                             status="estimated", isEstimate=True, notes=f"vendor_only_step;implied_D={_f(implied)};plausible(adjacent_median={_f(med)},nearest_gap={int(gaps.min())}d)"))
        else:
            n_unres += 1
            rows.append(_row(tradingItemId=tid, exDate=ex, eventType="other", legType="cash", cashPerShare=np.nan, cashCurrency="USD", fxToUSD=1.0,
                             source="vendor_implied", childValueSource="unknown",
                             evidence=f"vendor divAdjFactor step on {date.date()} implies D={_f(implied)} but ciqdividend has no row within ±7d and plausibility failed ({','.join(why) or 'no_amount'})",
                             status="unresolved", isEstimate=False, notes=f"vendor_only_step;no_ciqdividend_source;implied_D={_f(implied)};{','.join(why)}"))
    out = pd.DataFrame(rows)
    log.info("vendor-only step cells: %d candidates, %d had a ciqdividend source, %d estimated(vendor_implied, plausible), %d unresolved markers", len(c), n_src, n_est, n_unres)
    out.attrs["counts"] = {"candidates": int(len(c)), "with_source": n_src, "estimated_vendor_implied": n_est, "unresolved_marker": n_unres}
    out.attrs["candidates"] = c        # with prevDate filled from raw closes (L1 fix 4) -> build() writes it back so ledger_qa Q10 uses the same window
    return out


def lineage_ratio_check(linrows: pd.DataFrame, stock: pd.DataFrame, cash: pd.DataFrame, closes: RawCloses) -> pd.DataFrame:
    """F7 진단(R3 인계): rename/relist 행의 parentFactor 를 가격으로 검증 — (pf × 새 id exDate 종가 + Σ같은 날 자회사 가치 + 현금) / 옛 id 직전 종가.
    1 에서 25% 넘게 벗어나면 목록화(값은 바꾸지 않음)."""
    if linrows.empty:
        return pd.DataFrame()
    l = linrows.copy()
    new_ids = l.childTradingItemId.astype("Int64").fillna(-1).to_numpy(np.int64)
    p_old, d_old = closes.before(l.tradingItemId.to_numpy(np.int64), l.exDate.to_numpy())
    gap_ok = (l.exDate.to_numpy() - d_old).astype("timedelta64[D]").astype(float) <= 10
    p_old = np.where(gap_ok, p_old, np.nan)          # 옛 id 마지막 종가가 오래되면(체인 갭) 검증 불가
    p_new = closes.at(new_ids, l.exDate.to_numpy())
    p_new_next, d_next = closes.on_or_after(new_ids, l.exDate.to_numpy(), 7)
    p_new = np.where(np.isfinite(p_new), p_new, p_new_next)
    val = stock[stock.childValuePerOldShare.notna()].groupby(["tradingItemId", "exDate"]).childValuePerOldShare.sum() if len(stock) else pd.Series(dtype=float)
    csh = cash.groupby(["tradingItemId", "exDate"]).cashPerShare.sum() if len(cash) else pd.Series(dtype=float)
    rows = []
    for i, r in enumerate(l.itertuples(index=False)):
        if not (np.isfinite(p_old[i]) and np.isfinite(p_new[i]) and p_old[i] > 0):
            continue
        extra = float(val.get((int(r.tradingItemId), pd.Timestamp(r.exDate)), 0.0)) + float(val.get((int(new_ids[i]), pd.Timestamp(r.exDate)), 0.0)) \
            + float(csh.get((int(r.tradingItemId), pd.Timestamp(r.exDate)), 0.0)) + float(csh.get((int(new_ids[i]), pd.Timestamp(r.exDate)), 0.0))
        adj = (r.parentFactor * p_new[i] + extra) / p_old[i]
        if abs(adj - 1.0) > JUMP_TOL:
            rows.append({"oldId": int(r.tradingItemId), "newId": int(new_ids[i]), "exDate": r.exDate, "relation": r.eventType, "parentFactor": r.parentFactor,
                         "status": r.status, "oldClosePrev": p_old[i], "newCloseEx": p_new[i], "sameDayLegs": extra, "impliedRatio": (p_old[i] - extra) / p_new[i] if p_new[i] > 0 else np.nan,
                         "adjReturn": adj - 1.0, "notes": r.notes})
    out = pd.DataFrame(rows)
    log.info("lineage ratio check (F7): %d of %d rename/relist rows deviate > %.0f%%", len(out), len(l), JUMP_TOL * 100)
    return out


# --------------------------------------------------------------------------------------
# 사건 귀속(F4): exDate 에 거래되던 id 에만
# --------------------------------------------------------------------------------------
SAME_SECURITY_RELATIONS = ("rename", "relist")
# R1c ①: merger 관계인데 실제로는 같은 증권 라인(벤더가 옛 라인의 과거·배당을 새 id 에 이어붙임) — 사건 귀속에 한해 rename 과 같이 취급.
# (oldId, newId, 근거). V2 감사(reports/V2.md §4-2 ①): FRO 318283237→2665984 역합병 2015-11-30 비율 1.0, 옛 FRO 라인 2001~2011 배당 46 leg 의
# 이어붙임(정당, 스티치 공백 메움). 그 밖의 merger/acquisition 링크로는 사건을 다른 id 로 옮기지 않는다(TFC←TOBC 36 배당 등 오류).
SAME_SECURITY_LINK_OVERRIDES = [
    (318283237, 2665984, "FRO reverse merger 2015-11-30 ratio 1.0: same company line, vendor appended the old FRO history (V2 audit: legitimate stitched-line fill)"),
]


def same_security_pairs(lineage: pd.DataFrame) -> list[tuple[int, int]]:
    """사건 귀속에 쓸 수 있는 링크 = relation rename/relist + SAME_SECURITY_LINK_OVERRIDES (merger/acquisition 링크 제외, R1c ①)."""
    l = lineage[lineage.relation.astype(str).isin(SAME_SECURITY_RELATIONS)]
    pairs = list(zip(l.oldId.astype(int), l.newId.astype(int)))
    pairs += [(int(a), int(b)) for a, b, _ in SAME_SECURITY_LINK_OVERRIDES]
    return pairs


def _lineage_neighbors(lineage: pd.DataFrame, same_security_only: bool = True) -> dict:
    """id → 계보로 연결된 id 집합(양방향, 2 hop). same_security_only(기본): rename/relist(+override) 링크만 — merger 링크를 타고
    타사 라인으로 사건을 옮기지 않는다(R1c ①)."""
    nb = {}
    pairs = same_security_pairs(lineage) if same_security_only else list(zip(lineage.oldId.astype(int), lineage.newId.astype(int)))
    for a, b in pairs:
        nb.setdefault(a, set()).add(b)
        nb.setdefault(b, set()).add(a)
    out = {}
    for k, v in nb.items():
        ext = set(v)
        for x in v:
            ext |= nb.get(x, set())
        ext.discard(k)
        out[k] = ext
    return out


TRANSITION_WINDOW_DAYS = 7      # 옛 id 마지막 거래일 뒤 이 안의 사건 = 전환일 leg(옛 id 에 구주 기준으로 유지)
REASSIGNED_COPY_WINDOW_DAYS = 30  # X3 (a): 옮긴 행의 원래 id 에 exDate 전후 이 안에 종가가 있으면 stitched_copy 를 만든다


def attribute_to_trading_id(rows: pd.DataFrame, lifecycle: pd.DataFrame, lineage: pd.DataFrame, linrows: pd.DataFrame | None = None,
                            rc2_rules: bool = False, cal: "Calendar | None" = None) -> tuple[pd.DataFrame, list]:
    """현금·주식·권리 leg 를 exDate 에 거래되던 id 에 귀속한다(merger_*/other/quantity 행은 대상 아님).

    부모가 exDate 에 tradable(first ≤ ex ≤ last) 이면 유지. 아니면:
      · 전환일 leg(옛 id 마지막 거래일 < exDate ≤ +7 일이고 계보 승계 id 가 있음; 다중 leg 합병의 자회사 leg, IAC→MTCH 2020-07-01,
        VIA.B→CBS 2006-01-03, SLM→NAVI 2014-05-01 등): **옛 id 에 구주 기준으로 유지**(리드 결정: 엔진은 행 id 의 전일 가중치로 leg 를
        계상하고 승계 id 의 그 bar 가중치는 0; R2 는 링크된 첫 행에 옛 id 의 갭 사건을 적용). 승계 id 의 복사본은 버린다.
      · 그 밖(이어붙인 과거): 계보 이웃 중 tradable 한 id 가 같은 사건을 가지면 이 복사본을 버리고(history_appended_to_successor),
        없으면 그 id 로 옮기며(reassigned_from), tradable 한 이웃이 없으면 같은 사건을 가진 계보 id 중 firstTradeDate 가 가장 이른 id 만 남긴다.
    rc2f(X3 인계, rc2_rules=False 기본): (c) **현금 leg 한정**(R-X0 최종 확인 P1-8): 승계 id 가 링크일(= max(승계 id firstTradeDate, 옛 id 마지막
    거래일 다음 거래일); R2 가 승계 id 를 옛 id 에 연결하는 첫 행) 전에 자기 bar 를 가졌고 **그 승계 id 에 같은 현금 사건의 자기 기록이 있으면**(holder:
    CCI 2014-12-17 현금, 승계 첫 거래 12-16 < ex) 승계 id 의 사건이므로 옛 id 복사본을 드롭. 주식·권리 leg 는 어느 분기든 rc2 전환 규칙 그대로(옛 id 에
    구주 기준 유지, 승계 복사본 드롭) — 승계 id 행으로 두면 승계 id 자기 가격으로는 평가가 안 돼 unresolved 로 export 되고 엔진이 승계 보유자에게 잘못
    기장(VIA.B→CBS 2006-01-06 0.5×42.65, GGP 2010-11-10 0.983×36.9), 옛 id 가격으로 평가하면 승계 자기 행(WI, 이미 ex 가격)에 더해져 이중계상(2차 P0-2).
    when-issued 로 먼저 찍힌 승계(VIA.B→CBS.B 2005-12-08 WI, ex 2006-01-03 = 링크일)와 같은 날 전환(IAC→MTCH)은 그대로 옛 id 에 유지; (a) reassigned_from
    으로 옮긴 행(전환 창 밖의 이어붙인 과거)은 원래 id 의 이어붙인 이력에 stitched_copy 를 만들도록 dropped 기록(history_reassigned_copy:<new id>; build 가
    종가 유무로 거른다)."""
    if rows.empty:
        return rows, []
    lc = lifecycle.set_index("tradingItemId")
    first = lc.firstTradeDate.to_dict()
    last_raw = lc.lastTradeDate.to_dict()
    last_s = lc.lastTradeDate.copy()
    if "status" in lc.columns:
        last_s[lc.status == "active"] = pd.NaT                    # 진행 중 종목: 예고된 미래 배당락은 "종료 뒤" 가 아님
    last = last_s.to_dict()
    nb = _lineage_neighbors(lineage)                              # 같은 증권 링크(rename/relist + override)만, R1c ①
    nb_all = _lineage_neighbors(lineage, same_security_only=False)
    succ = {}                                                     # old → {new}(모든 relation; 전환일 leg 는 옛 id 에 남으므로 이동 아님)
    for a, b in zip(lineage.oldId.astype(int), lineage.newId.astype(int)):
        succ.setdefault(a, set()).add(b)
    r = rows.copy().reset_index(drop=True)
    target = (r.eventType.isin(["cash_dividend", "special_dividend", "spin_off", "rights", "warrant"]) & r.legType.isin(["cash", "stock", "rights"])) \
        | ((r.eventType == "other") & r.legType.isin(["stock", "rights"]) & (r.source == "distributions"))     # type-20 이지만 분류 불명(security_end 등)인 주식 leg

    def tradable(tid, ex):
        f, l = first.get(tid, pd.NaT), last.get(tid, pd.NaT)
        if pd.isna(f):
            return tid in first and (pd.isna(l) or ex <= l)
        return ex >= f and (pd.isna(l) or ex <= l)

    ck = r.childTradingItemId.map(lambda x: -1 if x is None or pd.isna(x) else int(x))
    sig = {}
    row_index = {}
    for i in np.flatnonzero(target.to_numpy()):
        key = (int(r.at[i, "tradingItemId"]), pd.Timestamp(r.at[i, "exDate"]), r.at[i, "legType"])
        val = round(float(r.at[i, "cashPerShare"]), 4) if r.at[i, "legType"] == "cash" and pd.notna(r.at[i, "cashPerShare"]) else (int(ck[i]), round(float(r.at[i, "childRatio"]), 4) if pd.notna(r.at[i, "childRatio"]) else None)
        sig.setdefault(key, set()).add(val)
        row_index.setdefault(key + (val,), i)
    drop = np.zeros(len(r), dtype=bool)
    dropped, n_reassigned, n_keep_nt, n_transition = [], 0, 0, 0
    new_ids = r.tradingItemId.to_numpy().copy()

    def drop_row(i, reason):
        drop[i] = True
        dropped.append({"tradingItemId": int(r.at[i, "tradingItemId"]), "exDate": pd.Timestamp(r.at[i, "exDate"]), "eventType": r.at[i, "eventType"],
                        "legType": r.at[i, "legType"], "childKey": int(ck[i]), "cashPerShare": r.at[i, "cashPerShare"], "childRatio": r.at[i, "childRatio"],
                        "source": r.at[i, "source"], "status": r.at[i, "status"], "reason": reason, "notes": r.at[i, "notes"], "evidence": r.at[i, "evidence"]})

    pred = {}                                                     # new → [(old, ratio)] (rename/relist 전환 비율: linrows 최종값)
    if linrows is not None and len(linrows):
        for rr in linrows.itertuples(index=False):
            if pd.notna(rr.childTradingItemId):
                pred.setdefault(int(rr.childTradingItemId), []).append((int(rr.tradingItemId), float(rr.parentFactor), pd.Timestamp(rr.exDate)))
    n_rekeyed = 0
    for i in np.flatnonzero(target.to_numpy()):
        if drop[i]:
            continue
        tid, ex = int(r.at[i, "tradingItemId"]), pd.Timestamp(r.at[i, "exDate"])
        if tradable(tid, ex):
            # 승계 id 첫 bar 에 기재된 전환일 leg(R3 QRTE.A 형): 옛 id 가 7 일 안에 종료됐으면 옛 id 로 옮기고 구주 기준으로 환산(× 전환 비율)
            f_new = first.get(tid, pd.NaT)
            if pd.notna(f_new) and pd.Timestamp(f_new) == ex:
                for old, ratio, eff in pred.get(tid, []):
                    l_old = last_raw.get(old, pd.NaT)
                    if pd.notna(l_old) and 0 < (ex - pd.Timestamp(l_old)).days <= TRANSITION_WINDOW_DAYS and abs((eff - ex).days) <= TRANSITION_WINDOW_DAYS:
                        leg = r.at[i, "legType"]
                        val = round(float(r.at[i, "cashPerShare"]), 4) if leg == "cash" and pd.notna(r.at[i, "cashPerShare"]) else (int(ck[i]), round(float(r.at[i, "childRatio"]), 4) if pd.notna(r.at[i, "childRatio"]) else None)
                        old_vals = sig.get((old, ex, leg), set())
                        if val in old_vals or (leg != "cash" and val[0] == -1 and any(v[0] == -1 for v in old_vals if isinstance(v, tuple))):
                            drop_row(i, f"successor_copy_of_transition_leg:kept_on_old_id_{old}")
                        else:
                            new_ids[i] = old
                            if np.isfinite(ratio) and ratio > 0 and ratio != 1.0:
                                for col in ("cashPerShare", "childRatio", "_cashEquiv"):
                                    if col in r.columns and pd.notna(r.at[i, col]):
                                        r.at[i, col] = float(r.at[i, col]) * ratio
                            r.at[i, "notes"] = str(r.at[i, "notes"]) + f";transition_day_leg(rekeyed_from_successor={tid};per_old_share_x{_f(ratio)})"
                            sig.setdefault((old, ex, leg), set()).add(val)
                            n_rekeyed += 1
                        break
            continue
        leg = r.at[i, "legType"]
        val = round(float(r.at[i, "cashPerShare"]), 4) if leg == "cash" and pd.notna(r.at[i, "cashPerShare"]) else (int(ck[i]), round(float(r.at[i, "childRatio"]), 4) if pd.notna(r.at[i, "childRatio"]) else None)
        l_old = last_raw.get(tid, pd.NaT)
        successors = [c for c in succ.get(tid, set()) if tradable(c, ex)]
        in_window = pd.notna(l_old) and 0 < (ex - pd.Timestamp(l_old)).days <= TRANSITION_WINDOW_DAYS
        if in_window and successors and not rc2_rules and leg == "cash":
            # X3 (c), holder branch, CASH legs only (R-X0 2nd review P0-2, final check P1-8): a successor that already had its own bar before the
            # link day (link day = max(successor first trade, next trading day after the old id's last trade); CCI 12-16 < ex 12-17) AND carries its
            # own record of this cash event is the trading id -> drop the old id's copy. A successor without its own record keeps the leg on the old
            # id (rc2 transition rule): moving it would add the leg to the successor's own row whose previous close (when-issued) is already ex.
            # Stock/rights legs always follow the rc2 transition rule: kept on the old id — a successor-keyed value leg cannot be valued with the
            # successor's own (already ex) prices, is exported unresolved and mis-booked to the successor's holders (CBS 2006-01-06, GGP 2010-11-10).
            nxt = pd.Timestamp(cal.align(np.array([np.datetime64(pd.Timestamp(l_old) + pd.Timedelta(days=1), "ns")]))[0][0]) if cal is not None else pd.Timestamp(l_old) + pd.Timedelta(days=1)
            traded_before = [c for c in successors if ex > max(pd.Timestamp(first.get(c)) if pd.notna(first.get(c, pd.NaT)) else nxt, nxt)]
            x3c_holder = [c for c in traded_before if val in sig.get((c, ex, leg), set())]
            if x3c_holder:
                drop_row(i, f"history_appended_to_successor:{x3c_holder[0]}")
                continue
        if in_window and successors:
            # 전환일 leg: 옛 id 에 구주 기준으로 유지, 승계 id 의 복사본은 제거
            r.at[i, "notes"] = str(r.at[i, "notes"]) + f";transition_day_leg(old_id_keyed;successor={successors[0]};old_lastTradeDate={_d(l_old)})"
            n_transition += 1
            for c in successors:
                j = row_index.get((c, ex, leg, val))
                if j is not None and not drop[j]:
                    drop_row(j, f"successor_copy_of_transition_leg:kept_on_old_id_{tid}")
            continue
        cands = [c for c in sorted(nb.get(tid, set())) if tradable(c, ex)]
        holder = [c for c in cands if val in sig.get((c, ex, leg), set())]
        if holder:
            drop_row(i, f"history_appended_to_successor:{holder[0]}")
        elif cands:
            new = cands[0]
            new_ids[i] = new
            sig.setdefault((new, ex, leg), set()).add(val)
            r.at[i, "notes"] = str(r.at[i, "notes"]) + f";reassigned_from={tid}(not_tradable_on_exDate)"
            n_reassigned += 1
            if not rc2_rules:
                # X3 (a): the original id keeps a stitched history in base_data -> record a copy request (keeper = the moved row on `new`)
                dropped.append({"tradingItemId": tid, "exDate": ex, "eventType": r.at[i, "eventType"], "legType": leg, "childKey": int(ck[i]),
                                "cashPerShare": r.at[i, "cashPerShare"], "childRatio": r.at[i, "childRatio"], "source": r.at[i, "source"], "status": r.at[i, "status"],
                                "reason": f"history_reassigned_copy:{new}", "notes": r.at[i, "notes"], "evidence": r.at[i, "evidence"]})
        else:
            holders = [c for c in nb.get(tid, set()) if val in sig.get((c, ex, leg), set())]
            if holders:
                keeper = min(holders + [tid], key=lambda c: (pd.Timestamp(first.get(c, pd.NaT)) if pd.notna(first.get(c, pd.NaT)) else pd.Timestamp("2200-01-01"), c))
                if keeper != tid:
                    drop_row(i, f"history_duplicate_no_trading_id:kept_on_{keeper}")
                    continue
            extra = ";no_same_security_lineage_link(merger_links_only)" if (nb_all.get(tid, set()) - nb.get(tid, set())) and not cands else ""
            r.at[i, "notes"] = str(r.at[i, "notes"]) + ";parent_not_tradable_on_exDate" + extra
            n_keep_nt += 1
    r["tradingItemId"] = new_ids
    r = r[~drop].reset_index(drop=True)
    log.info("attribution: dropped stitched copies %d, reassigned %d, kept on non-trading id %d, transition-day legs kept on old id %d, rekeyed from successor first bar %d",
             len(dropped), n_reassigned, n_keep_nt, n_transition, n_rekeyed)
    return r, dropped


# --------------------------------------------------------------------------------------
# 자회사 가치 평가(P10)
# --------------------------------------------------------------------------------------
def _implausible(check: float, drop: float) -> bool:
    """R1c ②: 사건 가치가 전일 종가의 15% 이상인데 부모 가격이 5% 도 안 떨어졌거나, 함의 일수익률이 40% 를 넘으면 불일치."""
    return bool(np.isfinite(check) and np.isfinite(drop) and check >= IMPLAUSIBLE_CHECK_MIN and (drop < IMPLAUSIBLE_DROP_MAX or check - drop > IMPLAUSIBLE_JUMP))


def _price_gap_date_search(closes: RawCloses, tid: int, child: int, ratio: float, ex: pd.Timestamp, split_pf: dict) -> tuple | None:
    """R1c ②: exDate 근처(−10/+45 달력일)에서 부모 하락(1 − P_d/P_{d−1})과 자회사 가치(ratio_d × C_d / P_{d−1})가 허용치 안에서 맞는 날 d 를 찾는다
    (부모 분할일 제외, 가치 ≥ 15%). ratio 는 exDate 직전 구주 기준이므로 d 의 보유 주식 기준으로 환산: d > ex 면 ex ≤ s ≤ d−1 의 분할 pf 로 나누고,
    d < ex 면 d ≤ s < ex 의 pf 를 곱한다. exDate 에 가장 가까운 날(동률이면 뒤) 반환 (d, C_d, P_prev_d, P_d, check, drop, ratio_d) 또는 None."""
    ps = closes.series(tid, ex - pd.Timedelta(days=DATE_SEARCH_BEFORE_DAYS + 7), ex + pd.Timedelta(days=DATE_SEARCH_AFTER_DAYS))
    cs = closes.series(child, ex - pd.Timedelta(days=DATE_SEARCH_BEFORE_DAYS + 7), ex + pd.Timedelta(days=DATE_SEARCH_AFTER_DAYS))
    if len(ps) < 2 or len(cs) == 0:
        return None
    best = None
    lo, hi = ex - pd.Timedelta(days=DATE_SEARCH_BEFORE_DAYS), ex + pd.Timedelta(days=DATE_SEARCH_AFTER_DAYS)
    sp = sorted((d_, pf_) for (t_, d_), pf_ in split_pf.items() if t_ == tid)
    for j in range(1, len(ps)):
        d = ps.index[j]
        if d == ex or d < lo or d > hi or (tid, d) in split_pf:
            continue
        c_d = cs.get(d, np.nan)
        p_d, p_prev = float(ps.iloc[j]), float(ps.iloc[j - 1])
        if not (np.isfinite(c_d) and p_prev > 0 and p_d > 0):
            continue
        ratio_d = ratio
        for s_, pf_ in sp:
            if d > ex and ex <= s_ <= ps.index[j - 1]:
                ratio_d /= pf_
            elif d < ex and d <= s_ < ex:
                ratio_d *= pf_
        check, drop = ratio_d * float(c_d) / p_prev, 1.0 - p_d / p_prev
        if check >= IMPLAUSIBLE_CHECK_MIN and abs(check - drop) <= VALUE_TOL_ABS + VALUE_TOL_REL * check:
            dist = abs((d - ex).days) - (0.5 if d > ex else 0.0)
            if best is None or dist < best[0]:
                best = (dist, d, float(c_d), p_prev, p_d, check, drop, ratio_d)
    return None if best is None else best[1:]


def value_children(rows: pd.DataFrame, closes: RawCloses, divcache: pd.DataFrame | None, cash_rows: pd.DataFrame,
                   split_rows: pd.DataFrame, id_set: set) -> pd.DataFrame:
    """stock/rights 행의 childValueSource·childValuePerOldShare·status 를 P10 순서로 채운다.

    ① child_close_ex: childRatio × 자회사 exDate raw 종가(base_data, 그 날 정확히)
    ③ event_value : co_acthist cash equivalent(구주 1주당, 벤더 평가액) — isEstimate
    ④ vendor_implied: ciqdividendcache 스핀 값(같은 날 현금은 차감) 또는 divadj 스텝 implied × P_prev — 부모 배당락 가격 갭
                     (1 − parentFactor×P_ex/P_prev) 과 ±(0.03 + 15%) 안에서 맞을 때만, isEstimate
    나머지 unknown → status unresolved. ① 인데 벤더/가격갭과 크게 어긋나면 estimated 로 내리고 notes 에 기록."""
    if rows.empty:
        return rows
    r = rows.copy()
    n = len(r)
    tid = r.tradingItemId.to_numpy(np.int64)
    ex = r.exDate.to_numpy()
    child = r.childTradingItemId.to_numpy()
    child_i = np.array([int(c) if c is not None and pd.notna(c) else -1 for c in child], dtype=np.int64)
    price_tid = tid                                   # (R-X0 2nd review P0-2: the old-id price bypass for moved legs is withdrawn; a leg is valued with its own row id's prices)
    p_ex = closes.at(price_tid, ex)
    p_prev, d_prev = closes.before(price_tid, ex)
    c_ex = np.where(child_i > 0, closes.at(np.where(child_i > 0, child_i, tid), ex), np.nan)
    c_next, d_next = closes.on_or_after(np.where(child_i > 0, child_i, tid), ex, 7)
    c_next = np.where(child_i > 0, c_next, np.nan)
    # 같은 날 부모 split 계수(가격 갭 계산용)·같은 날 현금
    pf = np.ones(n)
    if len(split_rows):
        sk = {(int(a), pd.Timestamp(b)): float(c) for a, b, c in zip(split_rows.tradingItemId, split_rows.exDate, split_rows.parentFactor)}
        pf = np.array([sk.get((int(a), pd.Timestamp(b)), 1.0) for a, b in zip(tid, ex)])
    cash_same = np.zeros(n)
    if len(cash_rows):
        ck = {(int(a), pd.Timestamp(b)): float(c) for a, b, c in zip(cash_rows.tradingItemId, cash_rows.exDate, cash_rows.cashPerShare)}
        cash_same = np.array([ck.get((int(a), pd.Timestamp(b)), 0.0) for a, b in zip(tid, ex)])
    drop_frac = 1.0 - pf * p_ex / p_prev                      # 부모가 잃은 가치 비율(시장 변동 포함)
    # 벤더 implied(ciqdividendcache): 같은 (id, dividenddate ±3일)
    vend = np.full(n, np.nan)
    vend_src = np.array([""] * n, dtype=object)
    if divcache is not None and len(divcache):
        dc = divcache.copy()
        dc["dividenddate"] = pd.to_datetime(dc.dividenddate)
        by = {k: g for k, g in dc.groupby("tradingitemid")}
        for i in range(n):
            g = by.get(int(tid[i]))
            if g is None:
                continue
            gap = (g.dividenddate - pd.Timestamp(ex[i])).abs().dt.days
            j = gap.idxmin()
            if gap[j] <= 3 and pd.notna(g.divamount[j]):
                v = float(g.divamount[j])
                if int(g.dividendpaymenttypeid[j]) == 5:          # Multiple: 같은 날 현금 포함 → 차감
                    v = v - cash_same[i]
                vend[i] = v
                vend_src[i] = f"ciqdividendcache type={int(g.dividendpaymenttypeid[j])} divamount={_f(g.divamount[j])}"
    impl = r["_impliedFrac"].to_numpy(dtype=float) if "_impliedFrac" in r.columns else np.full(n, np.nan)
    ceq = r["_cashEquiv"].to_numpy(dtype=float) if "_cashEquiv" in r.columns else np.full(n, np.nan)
    ratio = r.childRatio.to_numpy(dtype=float)
    legs_per_event = r.groupby(["tradingItemId", "exDate"]).exDate.transform("size").to_numpy()

    # 사건 단위 ① 합계(시세 leg) — ③④ 는 사건 전체 값이므로 잔여 = 사건값 − Σ시세 leg 를 미시세 leg 에 배분한다(F2a)
    priced = np.isfinite(ratio) & np.isfinite(c_ex) & (ratio > 0)
    ev_key = list(zip(tid.tolist(), pd.to_datetime(ex).tolist()))
    ev_df = pd.DataFrame({"k": ev_key, "priced_val": np.where(priced, ratio * c_ex, 0.0), "unpriced": (~priced).astype(int)})
    ev_sum = ev_df.groupby("k").priced_val.sum().to_dict()
    ev_unpriced = ev_df.groupby("k").unpriced.sum().to_dict()
    src_out, val_out, st_out, est_out, notes_out = [], [], [], [], []
    for i in range(n):
        notes = [r.notes.iloc[i]] if r.notes.iloc[i] else []
        vendor_frac = np.nan
        if np.isfinite(vend[i]) and np.isfinite(p_prev[i]) and p_prev[i] > 0:
            vendor_frac = vend[i] / p_prev[i]
        elif np.isfinite(impl[i]):
            vendor_frac = impl[i]
        corroborated = np.isfinite(vendor_frac) and np.isfinite(drop_frac[i]) and \
            abs(vendor_frac - drop_frac[i]) <= VALUE_TOL_ABS + VALUE_TOL_REL * abs(vendor_frac)
        k = ev_key[i]
        n_unpriced = max(int(ev_unpriced.get(k, 1)), 1)
        priced_sum = float(ev_sum.get(k, 0.0))
        in_base = child_i[i] in id_set if child_i[i] > 0 else False
        if child_i[i] > 0 and not in_base:
            notes.append("child_not_in_base_data")
        if priced[i]:
            val = ratio[i] * c_ex[i]
            src, st, est = "child_close_ex", "ok", False
            check = val / p_prev[i] if np.isfinite(p_prev[i]) and p_prev[i] > 0 else np.nan
            notes.append(f"childClose={_f(c_ex[i])};checkFrac={_f(check, 4)};dropFrac={_f(drop_frac[i], 4)};vendorFrac={_f(vendor_frac, 4)}")
            if np.isfinite(check) and np.isfinite(vendor_frac) and np.isfinite(drop_frac[i]) and legs_per_event[i] == 1:
                dev_check, dev_vendor = abs(check - drop_frac[i]), abs(vendor_frac - drop_frac[i])
                if abs(check - vendor_frac) > 0.10 and dev_vendor < dev_check and dev_check > VALUE_TOL_ABS + VALUE_TOL_REL * abs(check):
                    st, est = "estimated", True
                    notes.append("ratio_suspect:price_gap_sides_with_vendor")
                elif abs(check - vendor_frac) > 0.10:
                    notes.append("vendor_implied_mismatch(ratio_kept)")
        elif np.isfinite(ceq[i]) and ceq[i] > 0 and (ceq[i] - priced_sum) > 1e-9:
            val, src, st, est = float((ceq[i] - priced_sum) / n_unpriced), "event_value", "estimated", True
            check_ev = ceq[i] / p_prev[i] if np.isfinite(p_prev[i]) and p_prev[i] > 0 else np.nan
            notes.append(f"cash_equivalent_text={_f(ceq[i])}(event total);priced_legs={_f(priced_sum)};leg_share=1/{n_unpriced};checkFrac={_f(check_ev, 4)};dropFrac={_f(drop_frac[i], 4)}")
            if np.isfinite(c_next[i]):
                notes.append(f"child_first_close_after_ex={_f(c_next[i])}@{_d(d_next[i])}(not used, P10)")
        elif corroborated and (vendor_frac * p_prev[i] - priced_sum) > 1e-9:
            val, src, st, est = float((vendor_frac * p_prev[i] - priced_sum) / n_unpriced), "vendor_implied", "estimated", True
            notes.append(f"{vend_src[i] or 'divadj_step'};vendorFrac={_f(vendor_frac, 4)}(event total);priced_legs={_f(priced_sum)};leg_share=1/{n_unpriced};dropFrac={_f(drop_frac[i], 4)}")
        else:
            val, src, st, est = np.nan, "unknown", "unresolved", False
            why = []
            if not np.isfinite(ratio[i]):
                why.append("ratio_unknown")
            if child_i[i] <= 0:
                why.append("child_unknown")
            elif not np.isfinite(c_ex[i]):
                why.append("no_child_close_on_exDate" + (f"(first_after={_f(c_next[i])}@{_d(d_next[i])})" if np.isfinite(c_next[i]) else ""))
            if np.isfinite(vendor_frac):
                why.append(f"vendor_uncorroborated(vendorFrac={_f(vendor_frac, 4)},dropFrac={_f(drop_frac[i], 4)})" if not corroborated
                           else f"vendor_value_exhausted_by_priced_legs(vendorFrac={_f(vendor_frac, 4)},priced={_f(priced_sum)})")
            if np.isfinite(ceq[i]) and ceq[i] > 0 and (ceq[i] - priced_sum) <= 1e-9:
                why.append(f"cash_equivalent_exhausted_by_priced_legs(ce={_f(ceq[i])},priced={_f(priced_sum)})")
            notes.append(",".join(why) if why else "no_value_source")
        src_out.append(src); val_out.append(val); st_out.append(st); est_out.append(est); notes_out.append(";".join(notes))
    # R1c ②: P10 ①(child_close_ex)·③(event_value) 사건 가치의 가격 정합 검사(전환일 leg·manual_override 제외; 사건 단위 합계 기준).
    # 불일치 단일 leg ① 는 exDate 근처에서 부모 하락과 자회사 종가가 맞는 날로 교정(P10 ① 재유도), 그 밖은 unresolved(값 NaN, 표시).
    repairs = []
    ex_new = list(pd.to_datetime(ex))
    split_pf = {(int(a), pd.Timestamp(b)): float(c) for a, b, c in zip(split_rows.tradingItemId, split_rows.exDate, split_rows.parentFactor)} if len(split_rows) else {}
    ev_total = {}
    for i in range(n):
        if src_out[i] in ("child_close_ex", "event_value") and np.isfinite(val_out[i]):
            ev_total[ev_key[i]] = ev_total.get(ev_key[i], 0.0) + float(val_out[i])
    n_moved = n_unres = 0
    for i in range(n):
        if src_out[i] not in ("child_close_ex", "event_value") or r.source.iloc[i] == "manual_override" or "transition_day_leg" in notes_out[i] \
                or r.eventType.iloc[i] in MERGER_TYPES:                     # 합병 leg 는 부모가 통째로 전환(가격 하락 비교 무의미), 리드 결정 leg 는 제외
            continue
        if not (np.isfinite(p_prev[i]) and p_prev[i] > 0):
            continue
        check = ev_total.get(ev_key[i], np.nan) / p_prev[i]
        if not _implausible(check, drop_frac[i]):
            continue
        tag = f"checkFrac={_f(check, 4)},dropFrac={_f(drop_frac[i], 4)},rejected_value={_f(val_out[i])}"
        found = None
        if src_out[i] == "child_close_ex" and legs_per_event[i] == 1 and child_i[i] > 0 and np.isfinite(ratio[i]) and ratio[i] > 0:
            found = _price_gap_date_search(closes, int(price_tid[i]), int(child_i[i]), float(ratio[i]), pd.Timestamp(ex[i]), split_pf)
        if found is not None:
            d, c_d, pp, pe, chk, drp, ratio_d = found
            repairs.append({"tradingItemId": int(tid[i]), "exDate": pd.Timestamp(ex[i]), "field": "exDate", "before": _d(ex[i]), "after": _d(d),
                            "evidence": f"P10 ① value {_f(val_out[i])} inconsistent with the parent's own price move on {_d(ex[i])} ({tag}); parent drop and child close match on {_d(d)} "
                                        f"(checkFrac={_f(chk, 4)}, dropFrac={_f(drp, 4)}, child close {_f(c_d)}, parent {_f(pp)}->{_f(pe)}, ratio per share held at {_d(d)} {_f(ratio_d)}) (R1c, V2 §4-2 ②)", "source": "base_data"})
            ex_new[i] = pd.Timestamp(d)
            val_out[i] = float(ratio_d) * c_d
            ratio[i] = ratio_d                                     # 교정된 날의 보유 주식 기준(그 날 직전 구주) 비율
            p_prev[i], p_ex[i], c_ex[i] = pp, pe, c_d
            import re as _re                                   # 선두의 childClose/checkFrac/dropFrac 을 교정된 날 값으로(QA Q15 는 선두 값을 읽음)
            notes_out[i] = _re.sub(r"childClose=[^;]*;checkFrac=[^;]*;dropFrac=[^;]*", f"childClose={_f(c_d)};checkFrac={_f(chk, 4)};dropFrac={_f(drp, 4)}", notes_out[i], count=1)
            notes_out[i] = notes_out[i] + f";exDate_corrected_from={_d(ex[i])}(price_gap_match@{_d(d)};original {tag})"
            n_moved += 1
        else:
            repairs.append({"tradingItemId": int(tid[i]), "exDate": pd.Timestamp(ex[i]), "field": "childValuePerOldShare", "before": float(val_out[i]), "after": "NaN(unresolved)",
                            "evidence": f"P10 {'①' if src_out[i] == 'child_close_ex' else '③'} value inconsistent with the parent's own price move ({tag}); no matching date in "
                                        f"[-{DATE_SEARCH_BEFORE_DAYS},+{DATE_SEARCH_AFTER_DAYS}]d -> unresolved (R1c, V2 §4-2 ②)", "source": "base_data"})
            notes_out[i] = notes_out[i] + f";implausible_{src_out[i]}({tag};no_price_gap_match)"
            val_out[i], src_out[i], st_out[i], est_out[i] = np.nan, "unknown", "unresolved", False
            n_unres += 1
    if n_moved or n_unres:
        log.info("value plausibility (R1c): exDate corrected by price-gap match %d, set unresolved %d", n_moved, n_unres)
    r["exDate"] = pd.to_datetime(ex_new)
    r["childRatio"] = ratio
    r["childValueSource"] = src_out
    r["childValuePerOldShare"] = np.array(val_out, dtype=float)
    r["status"] = st_out
    r["isEstimate"] = est_out
    r["notes"] = notes_out
    r.attrs["repairs"] = repairs
    if "_r3_status" in r.columns:        # R3 인계 행: R3 가 estimated(비율 추정) 로 넘긴 행은 시세로 평가돼도 estimated·isEstimate 유지
        m = (r["_r3_status"] == "estimated") & (r.status == "ok")
        r.loc[m, "status"] = "estimated"
        r.loc[m, "isEstimate"] = True
        r.loc[m, "notes"] = r.loc[m, "notes"] + ";R3_ratio_estimated"
    r["_parentCloseBefore"] = p_prev
    r["_parentCloseEx"] = p_ex
    r["_childCloseEx"] = c_ex
    return r


def align_legs_to_settlement_bar(stock: pd.DataFrame, lifecycle: pd.DataFrame, events: pd.DataFrame, cal: Calendar, closes: "RawCloses | None" = None) -> tuple[pd.DataFrame, list]:
    """R-X4 §4-2 (lead decision, rc2f rules): a value leg (stock/rights) keyed on a parent that the engine settles through the events file
    (events row with a cash/stock leg) must be dated on the settlement bar = first trading day after lifecycle.lastTradeDate — a leg dated after
    that bar meets zero exposure in the engine (lost). Legs with settlement bar < exDate <= settlement bar + TRANSITION_WINDOW_DAYS are moved to
    the settlement bar (notes exDate_aligned_to_settlement_bar, repairs log field exDate). Later legs are left (listed by rc2f_recount).
    R-X0 final check P2-17: the move happens only when the child has a raw close ON the settlement bar (P10 ① values the leg at its exDate) — a leg
    moved to a bar without a child close would become unresolved and be lost in the export (2649817 2001-10-01 -> 09-25); otherwise the leg keeps
    its exDate (notes settlement_bar_alignment_skipped, repairs log) and rc2f_recount lists it as a leg after the settlement bar."""
    if stock.empty:
        return stock, []
    lc = lifecycle.set_index("tradingItemId")
    last = lc.lastTradeDate.copy()
    if "status" in lc.columns:
        last[lc.status == "active"] = pd.NaT
    ev = events[(events.hasCashLeg | events.hasStockLeg)] if events is not None and len(events) else events
    settled_ids = set(int(x) for x in ev.tradingItemId) if ev is not None and len(ev) else set()
    st = stock.copy()
    repairs = []
    n = 0
    for i in st.index[st.legType.isin(["stock", "rights"]) & st.tradingItemId.astype(int).isin(list(settled_ids))]:
        tid = int(st.at[i, "tradingItemId"])
        ltd = last.get(tid, pd.NaT)
        if pd.isna(ltd):
            continue
        bar = pd.Timestamp(cal.align(np.array([np.datetime64(pd.Timestamp(ltd) + pd.Timedelta(days=1), "ns")]))[0][0])
        ex = pd.Timestamp(st.at[i, "exDate"])
        if bar < ex <= bar + pd.Timedelta(days=TRANSITION_WINDOW_DAYS):
            child = st.at[i, "childTradingItemId"]
            if closes is not None and child is not None and pd.notna(child):
                c_bar = closes.at(np.array([int(child)]), np.array([np.datetime64(bar, "ns")]))[0]
                if not np.isfinite(c_bar):
                    st.at[i, "notes"] = (str(st.at[i, "notes"]) + ";" if st.at[i, "notes"] else "") + f"settlement_bar_alignment_skipped(no child {int(child)} close on {_d(bar)};lastTradeDate={_d(ltd)})"
                    repairs.append({"tradingItemId": tid, "exDate": ex, "field": "exDate", "before": _d(ex), "after": f"kept({_d(ex)})",
                                    "evidence": f"secondary leg after the settlement bar {_d(bar)} NOT moved: child {int(child)} has no raw close on that bar (the leg would become unresolved and be lost in the export, R-X0 P2-17); listed by rc2f_recount",
                                    "source": "rc2f_rule"})
                    continue
            st.at[i, "exDate"] = bar
            st.at[i, "notes"] = (str(st.at[i, "notes"]) + ";" if st.at[i, "notes"] else "") + f"exDate_aligned_to_settlement_bar(from={_d(ex)};lastTradeDate={_d(ltd)})"
            repairs.append({"tradingItemId": tid, "exDate": bar, "field": "exDate", "before": _d(ex), "after": _d(bar),
                            "evidence": f"secondary merger leg dated after the settlement bar {_d(bar)} (lifecycle lastTradeDate {_d(ltd)} + 1 trading day); the engine settles the parent on that bar (R-X4 §4-2)",
                            "source": "rc2f_rule"})
            n += 1
    if n:
        log.info("value legs aligned to the parent's settlement bar (R-X4 §4-2): %d", n)
    return st, repairs


def reconcile_split_same_day(split: pd.DataFrame, stock: pd.DataFrame, cash: pd.DataFrame, closes: RawCloses) -> pd.DataFrame:
    """분할일 raw 점프 불일치(raw_jump_mismatch)가 같은 날 스핀·현금으로 설명되면 ok 로 되돌린다:
    (parentFactor × P_ex + Σ childValuePerOldShare + cash) / P_prev ≈ 1 (± JUMP_TOL)."""
    if split.empty:
        return split
    s = split.copy()
    m = s.notes.str.contains("raw_jump_mismatch", na=False) & (s.status != "unresolved")
    if not m.any():
        return s
    val = stock[stock.childValuePerOldShare.notna()].groupby(["tradingItemId", "exDate"]).childValuePerOldShare.sum() if len(stock) else pd.Series(dtype=float)
    csh = cash.groupby(["tradingItemId", "exDate"]).cashPerShare.sum() if len(cash) else pd.Series(dtype=float)
    idx = s.index[m]
    p_ex = closes.at(s.loc[idx, "tradingItemId"], s.loc[idx, "exDate"])
    p_prev, _ = closes.before(s.loc[idx, "tradingItemId"], s.loc[idx, "exDate"])
    n_fixed = 0
    for i, pe, pp in zip(idx, p_ex, p_prev):
        key = (int(s.at[i, "tradingItemId"]), pd.Timestamp(s.at[i, "exDate"]))
        extra = float(val.get(key, 0.0)) + float(csh.get(key, 0.0))
        if extra <= 0 or not (np.isfinite(pe) and np.isfinite(pp) and pp > 0):
            continue
        adj = (s.at[i, "parentFactor"] * pe + extra) / pp
        if abs(adj - 1.0) <= JUMP_TOL:
            s.at[i, "status"] = "ok"
            s.at[i, "isEstimate"] = False
            s.at[i, "notes"] = str(s.at[i, "notes"]) + f";same_day_distribution_explains_gap(adj={_f(adj, 4)})"
            n_fixed += 1
    log.info("split rows explained by same-day distribution: %d", n_fixed)
    return s


# --------------------------------------------------------------------------------------
# 조립·검증·저장
# --------------------------------------------------------------------------------------
MERGER_TYPES = ("merger_cash", "merger_stock", "other")


def reconcile_merger_cash_duplicates(cash: pd.DataFrame, merger: pd.DataFrame, lifecycle: pd.DataFrame, closes: RawCloses) -> tuple[pd.DataFrame, pd.DataFrame, list, list]:
    """F1/R2-(3): 같은 종목의 ciqdividend 현금과 corporate_actions 현금 대가가 같은 금액(±1e-6, ±5 일)이면 진짜 중복 → 배당 행을 버리고
    merger 행만 남긴다(수익률 생성기는 merger_* 를 제외, 엔진은 events 파일 담당). 금액이 다르면 둘 다 남긴다(별개 leg).
    정산성 배당(exDate 가 lastTradeDate 전후 5 일 안, 금액 ≥ 0.9 × 직전 종가, 종료 사유 merger/delist/bankruptcy, merger 행 없음)은
    eventType merger_cash 로 재분류(source corporate_actions, notes settlement_like). 반환 (cash, merger, dropped, repairs)."""
    dropped, repairs = [], []
    if cash.empty:
        return cash, merger, dropped, repairs
    cash = cash.copy()
    mc = merger[merger.legType == "cash"] if len(merger) else merger
    by = {tid: g for tid, g in mc.groupby("tradingItemId")} if len(mc) else {}
    lc = lifecycle.set_index("tradingItemId")
    drop = np.zeros(len(cash), dtype=bool)
    last_arr = cash.tradingItemId.map(lc.lastTradeDate).to_numpy()
    near_end = np.abs((cash.exDate.to_numpy() - last_arr).astype("timedelta64[D]").astype(float)) <= 5
    cand = (cash.tradingItemId.isin(list(by.keys())) | near_end).to_numpy()
    p_prev = np.full(len(cash), np.nan)
    if cand.any():
        p_prev[cand], _ = closes.before(cash.tradingItemId.to_numpy(np.int64)[cand], cash.exDate.to_numpy()[cand])
    for i in np.flatnonzero(cand):
        r = cash.iloc[i]
        g = by.get(int(r.tradingItemId))
        amt = float(r.cashPerShare) if pd.notna(r.cashPerShare) else np.nan
        if g is not None and np.isfinite(amt):
            near = g[((g.exDate - r.exDate).abs().dt.days <= 5) & (np.abs(g.cashPerShare.astype(float) - amt) <= 1e-6 * max(1.0, amt))]
            if len(near):
                drop[i] = True
                dropped.append({"tradingItemId": int(r.tradingItemId), "exDate": r.exDate, "eventType": r.eventType, "legType": "cash", "childKey": -1,
                                "cashPerShare": amt, "childRatio": np.nan, "source": r.source, "status": r.status,
                                "reason": f"merger_settlement_duplicate:{near.eventType.iloc[0]}@{_d(near.exDate.iloc[0])}", "notes": r.notes, "evidence": r.evidence})
                continue
        if g is not None and ((g.exDate - r.exDate).abs().dt.days <= 5).any():
            continue                                        # corporate_actions 현금 대가가 따로 있음(금액 다름) → 배당 행은 별개 행으로 유지(VIAN)
        if "repairOf" in cash.columns and isinstance(r.repairOf, str) and "csv_override:" in r.repairOf:
            continue                                        # R-X0 P2-8: deliberate CSV corrections are not reclassified by the settlement-like rule
        if int(r.tradingItemId) in lc.index:
            last = lc.at[int(r.tradingItemId), "lastTradeDate"]
            reason = str(lc.at[int(r.tradingItemId), "endReason"]) if "endReason" in lc.columns else ""
            if pd.notna(last) and abs((pd.Timestamp(last) - r.exDate).days) <= 5 and np.isfinite(amt) and np.isfinite(p_prev[i]) and p_prev[i] > 0 \
                    and amt >= 0.9 * p_prev[i] and reason in ("merger", "delist_other", "bankruptcy", "liquidation"):
                cash.at[cash.index[i], "eventType"] = "merger_cash"
                cash.at[cash.index[i], "source"] = "corporate_actions"
                cash.at[cash.index[i], "notes"] = str(r.notes) + f";settlement_like_reclassified(cash {amt:.6g} >= 0.9 x P_prev {p_prev[i]:.6g}, lastTradeDate {_d(last)}, endReason {reason})"
                cash.at[cash.index[i], "evidence"] = str(r.evidence) + " | ciqdividend cash on/after the last trade = settlement (R2 review §8-4)"
                repairs.append({"tradingItemId": int(r.tradingItemId), "exDate": r.exDate, "field": "eventType", "before": r.eventType, "after": "merger_cash",
                                "evidence": f"settlement-like cash {amt:.6g} vs P_prev {p_prev[i]:.6g} at termination ({reason}, lastTradeDate {_d(last)}); excluded from returns v2 like other merger legs",
                                "source": "corporate_actions"})
    n_re = len(repairs)
    log.info("merger-cash reconciliation: true duplicates dropped %d, settlement-like dividends reclassified %d", int(drop.sum()), n_re)
    return cash[~drop].reset_index(drop=True), merger, dropped, repairs


def merge_rename_with_split(linrows: pd.DataFrame, split: pd.DataFrame, closes: RawCloses | None = None) -> tuple[pd.DataFrame, pd.DataFrame, list, list]:
    """R2-(2): 옛 id 의 같은 날 rename/relist 행과 split 행은 하나의 수량 전환이다. lineage 비율이 가정값(NaN→1.0)이면 split 계수를
    채택하고, 명시 비율이 split 계수와 같으면 split 행을 제거(rename 행이 대표), 다르면 둘 다 남기고 ratio_conflict 표시(F7 목록).
    R1c ③: 옛 id 가 경계일 이후에도 호가하면(raw 종가가 exDate 당일 또는 이후에 있음) split 행을 옛 id 에 **남긴다** — 옛 id 의 잔여 행의
    가격 step 은 그 행만이 설명한다(TWCA 119042293 2009-03-13 1:3, +206% flag 0 → +2.1%). rename 행은 비율만 채택(notes 표시)."""
    if linrows.empty or split.empty:
        return linrows, split, [], []
    lin = linrows.copy()
    sp = split.copy()
    key_sp = {(int(a), pd.Timestamp(b)): i for i, (a, b) in enumerate(zip(sp.tradingItemId, sp.exDate))}
    drop_sp = np.zeros(len(sp), dtype=bool)
    keep_quoting = np.zeros(len(sp), dtype=bool)
    conflicts, repairs = [], []
    n_kept_quoting = 0
    for i in lin.index:
        k = (int(lin.at[i, "tradingItemId"]), pd.Timestamp(lin.at[i, "exDate"]))
        j = key_sp.get(k)
        if j is None:
            continue
        pf_s = float(sp.iloc[j].parentFactor)
        pf_r = float(lin.at[i, "parentFactor"])
        assumed = "ratio_assumed_1.0" in str(lin.at[i, "notes"])
        quoting = False
        if closes is not None:
            c_after, d_after = closes.on_or_after([k[0]], [k[1]], max_days=100000)
            quoting = bool(np.isfinite(c_after[0]))
        if quoting and (assumed or abs(pf_r - pf_s) <= 1e-9 * max(1.0, pf_s)):
            keep_quoting[j] = True
            n_kept_quoting += 1
            sp.iloc[j, sp.columns.get_loc("notes")] = str(sp.iloc[j].notes) + f";kept_on_quoting_old_id(lineage_ratio_same;first_close_on_or_after={_d(d_after[0])})"
            lin.at[i, "notes"] = str(lin.at[i, "notes"]) + f";split_row_kept_on_old_id(quotes_on_or_after_boundary={_d(d_after[0])})"
            if assumed:
                lin.at[i, "parentFactor"] = pf_s
                lin.at[i, "status"] = "ok" if sp.iloc[j].status == "ok" else "estimated"
                lin.at[i, "isEstimate"] = lin.at[i, "status"] != "ok"
                lin.at[i, "notes"] = str(lin.at[i, "notes"]).replace("ratio_assumed_1.0", f"ratio_from_old_id_split_step={_f(pf_s)}")
                lin.at[i, "evidence"] = str(lin.at[i, "evidence"]) + f" | {sp.iloc[j].evidence}"
                repairs.append({"tradingItemId": k[0], "exDate": k[1], "field": "parentFactor", "before": pf_r, "after": pf_s,
                                "evidence": f"lineage ratio NaN; same-day split step on the old id {sp.iloc[j].evidence} carries the conversion (split row kept: old id quotes on/after the boundary, R1c)", "source": "ciqsplitcache"})
            continue
        if assumed:
            lin.at[i, "parentFactor"] = pf_s
            lin.at[i, "status"] = "ok" if sp.iloc[j].status == "ok" else "estimated"
            lin.at[i, "isEstimate"] = lin.at[i, "status"] != "ok"
            lin.at[i, "notes"] = str(lin.at[i, "notes"]).replace("ratio_assumed_1.0", f"ratio_from_old_id_split_step={_f(pf_s)}")
            lin.at[i, "evidence"] = str(lin.at[i, "evidence"]) + f" | {sp.iloc[j].evidence}"
            repairs.append({"tradingItemId": k[0], "exDate": k[1], "field": "parentFactor", "before": pf_r, "after": pf_s,
                            "evidence": f"lineage ratio NaN; same-day split step on the old id {sp.iloc[j].evidence} carries the conversion", "source": "ciqsplitcache"})
            drop_sp[j] = True
        elif abs(pf_r - pf_s) <= 1e-9 * max(1.0, pf_s):
            drop_sp[j] = True
        else:
            lin.at[i, "notes"] = str(lin.at[i, "notes"]) + f";ratio_conflict_with_split_step={_f(pf_s)}"
            sp.iloc[j, sp.columns.get_loc("notes")] = str(sp.iloc[j].notes) + f";ratio_conflict_with_lineage={_f(pf_r)}"
            conflicts.append({"oldId": k[0], "newId": lin.at[i, "childTradingItemId"], "exDate": k[1], "lineageRatio": pf_r, "splitStep": pf_s,
                              "relation": lin.at[i, "eventType"], "evidence": lin.at[i, "evidence"]})
    key_lin = {}
    for i, (a, b) in enumerate(zip(lin.tradingItemId, lin.exDate)):
        key_lin.setdefault((int(a), pd.Timestamp(b)), lin.index[i])
    for j in np.flatnonzero(drop_sp):
        i = key_lin[(int(sp.iloc[j].tradingItemId), pd.Timestamp(sp.iloc[j].exDate))]
        lin.at[i, "notes"] = str(lin.at[i, "notes"]) + f";represents_split_step({sp.iloc[j].evidence.split(';')[0]})"
    log.info("rename+split same-day: %d pairs, split rows represented by rename %d, kept on quoting old id %d, ratio conflicts %d",
             int(drop_sp.sum()) + n_kept_quoting + len(conflicts), int(drop_sp.sum()), n_kept_quoting, len(conflicts))
    return lin, sp[~drop_sp].reset_index(drop=True), conflicts, repairs


def stitched_copies_map(dropped: pd.DataFrame, ledger: pd.DataFrame, csv_exdate_moves: dict | None = None) -> pd.DataFrame:
    """R2 인계: 계보 복사본으로 버린 (id, exDate, leg) → 최종 keeper id(체인 추적) 와 keeper 의 eventId. 새 id 의 이어붙인 과거 행(base_data 에는
    시세가 있음)에서 그 사건을 찾을 때 이 표로 옛 id 행을 참조한다. 체인 끝에 원장 행이 없으면 keeperEventId 비움(의도적 제거: 자기 전환 leg 등).
    csv_exdate_moves {(keeperId, old exDate, old legType): (new exDate, new legType)}: CSV 가 keeper 의 exDate/legType 을 바꾼 경우 옛 키의 드롭 기록을
    새 키의 태그 keeper 로 매칭(R-X0 2차 P2-13, L1 수정 5); 복사본은 keeper 의 새 키로 재생성된다. keeper 를 못 찾은 행에는 keeperMissingReason
    (csv_rekeyed_keeper:<new key> / '') 를 남겨 Q9b 가 의도된 변경으로 분류한다."""
    csv_exdate_moves = csv_exdate_moves or {}
    import re as _re
    if dropped is None or not len(dropped):
        return pd.DataFrame(columns=["tradingItemId", "exDate", "legType", "childKey", "cashPerShare", "childRatio", "keeperId", "keeperEventId", "reason", "evidence"])
    d = dropped[dropped.reason.astype(str).str.contains("history_appended_to_successor|history_duplicate_no_trading_id|history_reassigned_copy|successor_copy_of_transition_leg", na=False)].copy()
    if d.empty:
        return pd.DataFrame(columns=["tradingItemId", "exDate", "legType", "childKey", "cashPerShare", "childRatio", "keeperId", "keeperEventId", "reason", "evidence"])
    d["exDate"] = pd.to_datetime(d.exDate)
    d["keeper0"] = d.reason.astype(str).map(lambda x: int(_re.findall(r"(\d+)$", x)[0]))
    ck = ledger.childTradingItemId.astype("Int64").fillna(-1)
    v = ledger[ledger.legType.isin(["cash", "stock", "rights"])]
    live = {}
    for eid, tid, ex, leg, amt, c, ratio in zip(v.eventId, v.tradingItemId, v.exDate, v.legType, v.cashPerShare, ck[v.index], v.childRatio):
        val = round(float(amt), 4) if leg == "cash" and pd.notna(amt) else (int(c), round(float(ratio), 4) if pd.notna(ratio) else None)
        live[(int(tid), pd.Timestamp(ex), leg, val)] = eid
    hop = {}
    for r in d.itertuples(index=False):
        val = round(float(r.cashPerShare), 4) if r.legType == "cash" and pd.notna(r.cashPerShare) else (int(r.childKey), round(float(r.childRatio), 4) if pd.notna(r.childRatio) else None)
        hop[(int(r.tradingItemId), pd.Timestamp(r.exDate), r.legType, val)] = int(r.keeper0)
    live_child = {}                                   # (id, exDate, child) → eventId (비율이 한쪽만 NaN 인 stock leg 도 같은 사건)
    for (tid, ex, leg, val), eid in live.items():
        if leg != "cash":
            live_child.setdefault((tid, ex, val[0]), eid)
    # R-X0 P1-1: keeper 행의 금액·자회사가 CSV 교정(repairOf csv_override:)으로 바뀌면 드롭 기록(옛 값)과 값으로 매칭되지 않는다 →
    # 같은 (id, exDate, legType) 의 csv_override 태그 keeper 가 정확히 1 행이면 그 행으로 매칭(복사본은 keeper 의 새 값으로 재생성)
    tagged = {}
    if "repairOf" in ledger.columns:
        vt = v[ledger.loc[v.index, "repairOf"].fillna("").astype(str).str.contains("csv_override:", regex=False)]
        for eid, tid, ex, leg in zip(vt.eventId, vt.tradingItemId, vt.exDate, vt.legType):
            tagged.setdefault((int(tid), pd.Timestamp(ex), leg), []).append(eid)
    keeper, keid = [], []
    n_tag = 0
    for r in d.itertuples(index=False):
        val = round(float(r.cashPerShare), 4) if r.legType == "cash" and pd.notna(r.cashPerShare) else (int(r.childKey), round(float(r.childRatio), 4) if pd.notna(r.childRatio) else None)
        k = int(r.keeper0)
        found = ""
        for _ in range(6):
            found = live.get((k, pd.Timestamp(r.exDate), r.legType, val), "") or (live_child.get((k, pd.Timestamp(r.exDate), val[0]), "") if r.legType != "cash" else "")
            if not found:
                cand = tagged.get((k, pd.Timestamp(r.exDate), r.legType), [])
                if not cand and (k, pd.Timestamp(r.exDate), r.legType) in csv_exdate_moves:
                    nk = csv_exdate_moves[(k, pd.Timestamp(r.exDate), r.legType)]
                    cand = tagged.get((k, nk[0], nk[1]), [])
                if len(cand) == 1:
                    found = cand[0]; n_tag += 1
            if found:
                break
            nxt = hop.get((k, pd.Timestamp(r.exDate), r.legType, val))
            if nxt is None or nxt == k:
                break
            k = nxt
        keeper.append(k)
        keid.append(found)
    if n_tag:
        log.info("stitched copies matched to CSV-corrected keepers by (id, exDate, legType): %d", n_tag)
    d["keeperId"] = keeper
    d["keeperEventId"] = keid
    d["evidence"] = d["evidence"] if "evidence" in d.columns else ""
    d["keeperMissingReason"] = ["" if e else (f"csv_rekeyed_keeper:{_d(csv_exdate_moves[(int(k), pd.Timestamp(x), l)][0])}/{csv_exdate_moves[(int(k), pd.Timestamp(x), l)][1]}"
                                             if (int(k), pd.Timestamp(x), l) in csv_exdate_moves else "")
                                for e, k, x, l in zip(keid, keeper, d.exDate, d.legType)]
    return d[["tradingItemId", "exDate", "legType", "childKey", "cashPerShare", "childRatio", "keeperId", "keeperEventId", "keeperMissingReason", "reason", "evidence"]].reset_index(drop=True)


def mark_estimated_price_split_rows(split: pd.DataFrame, est: pd.DataFrame, closes: "RawCloses") -> tuple[pd.DataFrame, int]:
    """R-Y2 P1 ①: a split row whose raw corroboration used a price inside an ESTIMATED price-override window (rc2f price_overrides isEstimate true:
    old-unit prints x 1/pf) is at best an estimate: status ok -> estimated, isEstimate True, notes corroborated_with_estimated_prices:<overrideId>.
    The window covers the exDate close and the previous close (both enter the raw-jump check)."""
    if split.empty or est is None or not len(est):
        return split, 0
    win = [(int(float(r.tradingItemId)), pd.Timestamp(r.dateFrom), pd.Timestamp(r.dateTo), r.overrideId) for r in est.itertuples(index=False)]
    by_tid: dict[int, list] = {}
    for tid, d0, d1, oid in win:
        by_tid.setdefault(tid, []).append((d0, d1, oid))
    s = split.copy()
    n = 0
    for i in s.index[s.tradingItemId.astype(int).isin(list(by_tid))]:
        tid = int(s.at[i, "tradingItemId"]); ex = pd.Timestamp(s.at[i, "exDate"])
        _, d_prev = closes.before(np.array([tid]), np.array([np.datetime64(ex, "ns")]))
        d_prev = pd.Timestamp(d_prev[0]) if not pd.isna(d_prev[0]) else pd.NaT
        hits = [oid for d0, d1, oid in by_tid[tid] if (d0 <= ex <= d1) or (pd.notna(d_prev) and d0 <= d_prev <= d1)]
        if hits and str(s.at[i, "status"]) == "ok":
            s.at[i, "status"] = "estimated"; s.at[i, "isEstimate"] = True
            s.at[i, "notes"] = (str(s.at[i, "notes"]) + ";" if s.at[i, "notes"] else "") + "corroborated_with_estimated_prices:" + ",".join(hits)
            prev = s.at[i, "repairOf"]
            tag = "price_override_estimate:" + ",".join(hits)
            s.at[i, "repairOf"] = tag if not prev else f"{prev};{tag}"
            n += 1
    return s, n


STITCHED_COPY_REASONS = ("history_appended_to_successor", "history_duplicate_no_trading_id", "history_reassigned_copy")   # 전환일 승계 복사본은 제외


def drop_csv_adds_duplicating_copies(ledger: pd.DataFrame, csv_ovr: list, csv_results: dict) -> tuple[pd.DataFrame, list]:
    """L1 통합 수정 (1): CSV `add` 행(repairOf csv_override:<oid>, action add) 이 stitched_copy 행과 같은 계약 키(id, exDate, eventType, legType, child)에
    있고 금액이 같으면(현금 ±1e-6 상대, 주식/권리 childRatio ±1e-6) CSV 행을 버리고 결과를 applied `duplicate_of_existing(stitched_copy …)(no-op)` 로
    갱신한다(어느 id 가 export 되는지는 keeper = 거래 id 규칙; 복사본은 exportExclude). 금액이 다르면 둘 다 남기고 reason 에 conflict 표시.
    반환 (ledger, dropped 기록 목록)."""
    import ledger_repairs as rep
    add_oids = {o["overrideId"] for o in csv_ovr if o["action"] == "add"}
    tags = ledger.repairOf.map(lambda x: [t.split(":", 1)[1] for t in x.split(";") if t.startswith(rep.CSV_TAG + ":")] if isinstance(x, str) else [])
    is_add = tags.map(lambda ts: any(t in add_oids for t in ts)) & (ledger.source != "stitched_copy") if add_oids else pd.Series(False, index=ledger.index)
    ck = ledger.childTradingItemId.astype("Int64").fillna(-1).astype("int64")
    copies = ledger[ledger.source == "stitched_copy"]
    by_key = {}
    for i, t, d, e, l, c in zip(copies.index, copies.tradingItemId, copies.exDate, copies.eventType, copies.legType, ck[copies.index]):
        by_key.setdefault((int(t), pd.Timestamp(d), e, l, int(c)), []).append(i)
    drop_idx, records = [], []
    for i in ledger.index[is_add]:
        r = ledger.loc[i]
        k = (int(r.tradingItemId), pd.Timestamp(r.exDate), r.eventType, r.legType, int(ck[i]))
        for j in by_key.get(k, []):
            c = ledger.loc[j]
            if r.legType == "cash":
                same = pd.notna(r.cashPerShare) and pd.notna(c.cashPerShare) and abs(float(r.cashPerShare) - float(c.cashPerShare)) <= 1e-6 * max(1.0, abs(float(c.cashPerShare)))
            elif r.legType in ("stock", "rights"):
                same = (pd.isna(r.childRatio) and pd.isna(c.childRatio)) or (pd.notna(r.childRatio) and pd.notna(c.childRatio) and abs(float(r.childRatio) - float(c.childRatio)) <= 1e-6 * max(1.0, abs(float(c.childRatio))))
            else:
                same = abs(float(r.parentFactor) - float(c.parentFactor)) <= 1e-6 * max(1.0, abs(float(c.parentFactor)))
            oids = [t for t in tags[i] if t in add_oids]
            if same:
                drop_idx.append(i)
                records.append({"tradingItemId": int(r.tradingItemId), "exDate": pd.Timestamp(r.exDate), "eventType": r.eventType, "legType": r.legType, "childKey": int(ck[i]),
                                "cashPerShare": r.cashPerShare, "childRatio": r.childRatio, "source": r.source, "status": r.status,
                                "reason": f"csv_add_duplicate_of_stitched_copy:{c.eventId}", "notes": r.notes, "evidence": r.evidence})
                for oid in oids:
                    res = csv_results.get(oid)
                    if res is not None:
                        res.update(status="applied", reason=f"duplicate_of_existing(stitched_copy {c.eventId} of keeper {c.keeperEventId}, same amount)(no-op)", affected_rows=0, noop=True,
                                   eventIds_override=str(c.eventId))
            else:
                for oid in oids:
                    res = csv_results.get(oid)
                    if res is not None and "conflict_with_stitched_copy" not in str(res.get("reason", "")):
                        res["reason"] = str(res.get("reason", "")) + f";conflict_with_stitched_copy({c.eventId}: different amount, both rows kept -> Q1)"
            break
    if drop_idx:
        log.info("csv add rows duplicating stitched copies dropped (override -> no-op): %d", len(drop_idx))
        ledger = ledger.drop(index=drop_idx).reset_index(drop=True)
    # a `set` still rejected as not_found whose intended result row now exists (the stitched copy of the keeper carries the real row the set
    # meant to create from a vendor marker the rebuild no longer makes: X3 marker replacements) -> no-op as well
    n_set = 0
    for o in csv_ovr:
        res = csv_results.get(o["overrideId"])
        if o["action"] == "set" and res is not None and res.get("status") == "rejected" and str(res.get("reason", "")).startswith("not_found"):
            hit = rep._result_row_exists({"ledger": ledger}, o)
            if hit:
                res.update(status="applied", reason=f"no_op(target_absent; result_row_exists: {hit})", affected_rows=0, noop=True)
                n_set += 1
    if n_set:
        log.info("csv set overrides with an absent target whose result row exists (stitched copy) -> no-op: %d", n_set)
    return ledger, records


def make_stitched_copies(ledger: pd.DataFrame, stitched: pd.DataFrame) -> pd.DataFrame:
    """계보 복사본 → stitched_copy 행(keeper 행 복제: tradingItemId 만 복사 id, source=stitched_copy, keeperEventId, exportExclude=True).
    keeper 행이 없는 복사본(의도적 제거) 과 전환일 승계 복사본은 만들지 않는다."""
    if stitched is None or not len(stitched):
        return pd.DataFrame(columns=ledger.columns)
    st = stitched[stitched.reason.astype(str).str.split(":").str[0].isin(STITCHED_COPY_REASONS) & (stitched.keeperEventId.fillna("").astype(str) != "")]
    if st.empty:
        return pd.DataFrame(columns=ledger.columns)
    by_eid = ledger.set_index("eventId")
    keep = by_eid.loc[st.keeperEventId.astype(str)].reset_index()
    copies = keep.copy()
    copies["tradingItemId"] = st.tradingItemId.to_numpy(np.int64)
    copies["keeperEventId"] = st.keeperEventId.astype(str).to_numpy()
    copies["source"] = "stitched_copy"
    copies["exportExclude"] = True
    copies["repairOf"] = None
    copies["notes"] = "stitched_copy_of=" + copies.keeperEventId + ";" + st.reason.astype(str).to_numpy() + ";keeper_notes:" + keep.notes.fillna("").astype(str).str[:120]
    copies["evidence"] = "stitched history copy of " + copies.keeperEventId + " | " + keep.evidence.fillna("").astype(str).str[:200]
    # eventId: 복사 id 기준 seq(기존 행 이후 번호)
    existing = ledger.groupby(["tradingItemId", "exDate", "eventType"]).size()
    grp = copies.groupby(["tradingItemId", "exDate", "eventType"]).cumcount()
    base = pd.Series([int(existing.get((int(t), pd.Timestamp(d), e), 0)) for t, d, e in zip(copies.tradingItemId, copies.exDate, copies.eventType)], index=copies.index)
    copies["eventId"] = [f"{int(t)}_{pd.Timestamp(d).strftime('%Y%m%d')}_{e}_{int(q)}" for t, d, e, q in zip(copies.tradingItemId, copies.exDate, copies.eventType, base + grp)]
    copies = copies.drop_duplicates(["tradingItemId", "exDate", "legType", "childTradingItemId", "eventType"])
    return copies[ledger.columns]


TRANSITION_COPY_REASON = "transition_leg_mirrored_to_stitched_successor"
SAME_SERIES_DAYS, SAME_SERIES_TOL = 3, 1e-4


def stitched_same_series(closes: RawCloses, old: int, succ: int, ex, first_succ=pd.NaT) -> tuple[bool, str]:
    """R1c ④: 승계 id S 의 base_data 가 전환일 ex 직전에 옛 id 의 종가를 **그대로 이어붙인**(같은 단위) 과거를 갖는지 — 옛 id 의 ex 직전 관측 종가 3 일이
    S 의 같은 날 종가와 1e-4 안에서 일치하고, 그 날들이 S 의 lifecycle firstTradeDate 이전이면 True. (LINT.A 0.05·TRXX→283620484 처럼 승계 id 의
    이어붙인 과거가 다른 증권·다른 단위이면 False: 구주 기준 leg 를 미러하면 틀린다.) 반환 (판정, 사유)."""
    ex = pd.Timestamp(ex)
    so = closes.series(old, ex - pd.Timedelta(days=40), ex - pd.Timedelta(days=1))
    if len(so) < SAME_SERIES_DAYS:
        return False, "old_id_fewer_than_3_closes_before_ex"
    so = so.iloc[-SAME_SERIES_DAYS:]
    if pd.notna(first_succ) and so.index[-1] >= pd.Timestamp(first_succ):
        return False, "successor_own_trading_before_ex"
    sv = closes.at([succ] * len(so), so.index)
    if not np.isfinite(sv).all():
        return False, "successor_has_no_stitched_close_on_old_id_dates"
    rel = np.abs(sv - so.to_numpy()) / np.maximum(np.abs(so.to_numpy()), 1e-12)
    if (rel > SAME_SERIES_TOL).any():
        return False, f"successor_stitched_closes_differ(max_rel={rel.max():.3g})"
    return True, "same_series"


def make_transition_copies(ledger: pd.DataFrame, closes: RawCloses, lifecycle: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """R1c ④(V2 §4-2 ④): 옛 id 에 구주 기준으로 남긴 전환일 value leg(cash/stock/rights, notes transition_day_leg(...successor=S))를, 승계 id S 의
    base_data 가 전환일 직전 종가를 **이어붙인 과거**(lifecycle firstTradeDate 이전)로 갖는 경우 S 에 stitched_copy 행으로 미러한다 — S 의
    전환일 행 수익률이 옛 id 와 같은 경제적 수익률이 되도록(NAVI 2014-05-01 −34.0% → 새 SLM 9.02+0.43 반영). exportExclude=true(엔진 패널은
    lifecycle 절단이라 전환일 전 보유 불가 → 이중 계상 없음). S 가 전환일 전에 자기 거래를 가진 경우(overlap)·같은 키 행이 이미 있는 경우는 제외.
    반환 (복사 행, 인계 표 행: ca_ledger_stitched_copies.csv 형식 + copyEventId)."""
    empty_map = pd.DataFrame(columns=["tradingItemId", "exDate", "legType", "childKey", "cashPerShare", "childRatio", "keeperId", "keeperEventId", "reason", "evidence", "copyEventId"])
    src = ledger[(ledger.source != "stitched_copy") & ledger.legType.isin(["cash", "stock", "rights"]) & ledger.notes.str.contains("transition_day_leg", na=False)]
    if src.empty:
        return pd.DataFrame(columns=ledger.columns), empty_map
    succ = src.notes.str.extract(r"transition_day_leg\((?:old_id_keyed;successor=(\d+)|rekeyed_from_successor=(\d+))")
    succ_id = succ[0].fillna(succ[1])
    first = lifecycle.set_index("tradingItemId").firstTradeDate.to_dict()
    ck_all = ledger.childTradingItemId.astype("Int64").fillna(-1)
    existing = set(zip(ledger.tradingItemId.astype(int), pd.to_datetime(ledger.exDate), ledger.legType, ck_all.astype(int)))
    src = src.assign(_succ=succ_id.to_numpy())
    src = src[src._succ.notna()]
    if src.empty:
        return pd.DataFrame(columns=ledger.columns), empty_map
    sid = src._succ.astype(int).to_numpy(np.int64)
    p_before, d_before = closes.before(sid, src.exDate.to_numpy())
    keep, notes_new, maps = [], [], []
    n_no_prev = n_overlap = n_exists = n_diff = 0
    skipped = []
    for k, (idx, row) in enumerate(src.iterrows()):
        s_ = int(sid[k])
        ex = pd.Timestamp(row.exDate)
        if not np.isfinite(p_before[k]):
            n_no_prev += 1
            continue
        f = first.get(s_, pd.NaT)
        if pd.notna(f) and pd.Timestamp(d_before[k]) >= pd.Timestamp(f):
            n_overlap += 1                                    # 승계 id 가 전환일 전에 자기 거래를 가짐(이어붙인 과거 아님)
            continue
        same, why = stitched_same_series(closes, int(row.tradingItemId), s_, ex, f)
        if not same:
            n_diff += 1                                       # 이어붙인 과거가 옛 id 의 시계열이 아님(다른 증권·단위) → 미러하지 않음(R3 검토 목록)
            skipped.append({"keeperEventId": row.eventId, "successor": s_, "reason": why})
            continue
        ck = int(row.childTradingItemId) if pd.notna(row.childTradingItemId) else -1
        if (s_, ex, row.legType, ck) in existing:
            n_exists += 1
            continue
        old = int(row.tradingItemId)
        keep.append(idx)
        notes_new.append(f"stitched_transition_copy_of={row.eventId};{TRANSITION_COPY_REASON}:{old};stitched_prev_close={_f(p_before[k])}@{_d(d_before[k])};"
                         f"keeper_notes:" + str(row.notes)[:120])
        maps.append({"tradingItemId": s_, "exDate": ex, "legType": row.legType, "childKey": ck, "cashPerShare": row.cashPerShare, "childRatio": row.childRatio,
                     "keeperId": old, "keeperEventId": row.eventId, "reason": f"{TRANSITION_COPY_REASON}:{old}", "evidence": str(row.evidence)[:200]})
        existing.add((s_, ex, row.legType, ck))
    log.info("transition-day legs mirrored onto stitched successor lines: %d (no stitched prev %d, successor own trading before ex %d, stitched series differs %d, key exists %d)",
             len(keep), n_no_prev, n_overlap, n_diff, n_exists)
    empty_map.attrs["skipped"] = skipped
    if not keep:
        return pd.DataFrame(columns=ledger.columns), empty_map
    copies = src.loc[keep].drop(columns=["_succ"]).copy().reset_index(drop=True)
    copies["tradingItemId"] = np.array([m["tradingItemId"] for m in maps], dtype=np.int64)
    copies["keeperEventId"] = copies.eventId.to_numpy()
    copies["source"] = "stitched_copy"
    copies["exportExclude"] = True
    copies["repairOf"] = None
    copies["notes"] = notes_new
    copies["evidence"] = "transition-day leg mirrored onto the stitched successor line (R1c 4, V2 4-2 4) | " + copies.evidence.fillna("").astype(str).str[:200]
    existing_n = ledger.groupby(["tradingItemId", "exDate", "eventType"]).size()
    grp = copies.groupby(["tradingItemId", "exDate", "eventType"]).cumcount()
    base = pd.Series([int(existing_n.get((int(t), pd.Timestamp(d), e), 0)) for t, d, e in zip(copies.tradingItemId, copies.exDate, copies.eventType)], index=copies.index)
    copies["eventId"] = [f"{int(t)}_{pd.Timestamp(d).strftime('%Y%m%d')}_{e}_{int(q)}" for t, d, e, q in zip(copies.tradingItemId, copies.exDate, copies.eventType, base + grp)]
    m = pd.DataFrame(maps)
    m["copyEventId"] = copies.eventId.to_numpy()
    m = m[empty_map.columns]
    m.attrs["skipped"] = skipped
    return copies[ledger.columns], m


def finalize(parts: list[pd.DataFrame]) -> pd.DataFrame:
    """행 결합 → 키 중복은 source 우선순위로 해소(키 = (id, exDate, legType, child) + merger 여부: 같은 날 현금배당과 합병 현금은 별개 행)
    → eventId 부여 → 계약 dtype."""
    clean = []
    for p in parts:
        if p is not None and len(p):
            p = p.copy()
            p.attrs = {}                       # 부속 attrs(dropped/repairs 목록)는 concat 비교·parquet 메타데이터에서 제외
            clean.append(p)
    df = pd.concat(clean, ignore_index=True)
    for c in LEDGER_COLUMNS:
        if c != "eventId" and c not in df.columns:
            df[c] = None
    df["childKey"] = df.childTradingItemId.map(lambda x: -1 if x is None or pd.isna(x) else int(x))
    df["mergerGroup"] = df.eventType.isin(MERGER_TYPES).astype(int)
    if "_prio" not in df.columns:
        df["_prio"] = np.nan
    df["prio"] = np.where(df["_prio"].notna(), df["_prio"], df.source.map(lambda s: SOURCE_PRIORITY.get(str(s).split(":")[0], 5)))
    df = df.sort_values(["tradingItemId", "exDate", "legType", "childKey", "mergerGroup", "prio"], kind="stable")
    dup = df.duplicated(["tradingItemId", "exDate", "legType", "childKey", "mergerGroup"], keep="first")
    n_dup = int(dup.sum())
    dropped = df[dup][["tradingItemId", "exDate", "eventType", "legType", "childKey", "cashPerShare", "childRatio", "source", "status", "notes"]].copy()
    dropped["reason"] = "key_collision(lower_priority_source)"
    dropped["evidence"] = df[dup]["evidence"].values
    df = df[~dup].copy()
    df["seq"] = df.groupby(["tradingItemId", "exDate", "eventType"]).cumcount()
    df["eventId"] = [f"{int(t)}_{pd.Timestamp(d).strftime('%Y%m%d')}_{e}_{int(s)}"
                     for t, d, e, s in zip(df.tradingItemId, df.exDate, df.eventType, df.seq)]
    bad_et = set(df.eventType) - set(EVENT_TYPES)
    bad_lt = set(df.legType) - set(LEG_TYPES)
    if bad_et or bad_lt:
        raise ValueError(f"unknown eventType {bad_et} / legType {bad_lt}")
    out = pd.DataFrame({
        "eventId": df.eventId.astype(str), "tradingItemId": df.tradingItemId.astype(np.int64),
        "exDate": pd.to_datetime(df.exDate).astype("datetime64[ns]"), "eventType": df.eventType.astype(str),
        "legType": df.legType.astype(str), "cashPerShare": df.cashPerShare.astype(float),
        "cashCurrency": df.cashCurrency.astype(object).where(df.cashCurrency.notna(), None),
        "fxToUSD": df.fxToUSD.astype(float), "parentFactor": df.parentFactor.astype(float),
        "childTradingItemId": pd.array([None if k < 0 else int(k) for k in df.childKey], dtype="Int64"),
        "childRatio": df.childRatio.astype(float), "childValueSource": df.childValueSource.astype(str),
        "childValuePerOldShare": df.childValuePerOldShare.astype(float), "subscriptionPrice": df.subscriptionPrice.astype(float),
        "expiryDate": pd.to_datetime(df.expiryDate).astype("datetime64[ns]"), "exerciseRatio": df.exerciseRatio.astype(float),
        "payDate": pd.to_datetime(df.payDate).astype("datetime64[ns]"), "recordDate": pd.to_datetime(df.recordDate).astype("datetime64[ns]"),
        "announcedDate": pd.to_datetime(df.announcedDate).astype("datetime64[ns]"), "source": df.source.astype(str),
        "evidence": df.evidence.fillna("").astype(str), "status": df.status.astype(str),
        "isEstimate": df.isEstimate.astype(bool) & (df.status != "unresolved"),
        "repairOf": df.repairOf.astype(object).where(df.repairOf.notna(), None), "notes": df.notes.fillna("").astype(str),
        "keeperEventId": df.keeperEventId.astype(object).where(df.keeperEventId.notna(), None) if "keeperEventId" in df.columns else None,
        "exportExclude": df.exportExclude.fillna(False).astype(bool) if "exportExclude" in df.columns else False,
    }, index=df.index)
    out = out.sort_values(["tradingItemId", "exDate", "legType", "childTradingItemId"], kind="stable").reset_index(drop=True)
    out.attrs["n_duplicates_dropped"] = n_dup
    out.attrs["dropped"] = dropped
    return out


def write_ledger(df: pd.DataFrame, path: Path) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    df = df[LEDGER_COLUMNS].copy()
    df.attrs = {}
    t = pa.Table.from_pandas(df, schema=LEDGER_SCHEMA, preserve_index=False)
    pq.write_table(t, path, compression="zstd")
    return {"rows": int(len(df)), "bytes": path.stat().st_size, "sha256": sha256(path)}


def write_repairs(repairs: list[dict], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    rep = pd.DataFrame(repairs, columns=REPAIR_COLUMNS) if repairs else pd.DataFrame(columns=REPAIR_COLUMNS)
    rep["exDate"] = pd.to_datetime(rep.exDate).dt.strftime("%Y-%m-%d")
    rep = rep.sort_values(["tradingItemId", "exDate", "field"], kind="stable")
    rep.to_csv(path, index=False)
    return len(rep)


def universe_flags(df: pd.DataFrame, membership: pd.DataFrame) -> pd.DataFrame:
    """exDate 직전(포함) 멤버십: Top500 / Top1000 bool 컬럼(리포트·QA 용, 원장 파일에는 넣지 않음)."""
    out = df[["tradingItemId", "exDate"]].copy()
    for u in ("Top500", "Top1000"):
        m = membership[membership.universe == u]
        by = {tid: g[["fromDate", "toDate"]].to_numpy() for tid, g in m.groupby("tradingItemId")}
        flag = np.zeros(len(df), dtype=bool)
        for i, (tid, ex) in enumerate(zip(df.tradingItemId, df.exDate)):
            g = by.get(int(tid))
            if g is None:
                continue
            ex64 = np.datetime64(pd.Timestamp(ex), "ns")
            flag[i] = bool(((g[:, 0] <= ex64) & (g[:, 1] >= ex64)).any())
        out[u] = flag
    return out


R3_STAGE = SCRATCH / "stage_rc2" / "R3"
R2_GAP_CANDIDATES = [SCRATCH / "stage_rc2" / "R2" / "qa" / "returns_v2" / "ledger_gap_candidates.csv",
                     SCRATCH / "stage_rc2" / "R2" / "review" / "vendor_step_no_ledger_172.csv"]


def build(root: Path, stage: Path, base_data: Path, out: Path | None = None, pull: bool = True, lineage_path=None, lifecycle_path=None,
          aux_base_data=None, r3_events=None, gap_candidates=None, overrides=None, overrides_ref_ledger=None, rc2_rules: bool = False,
          split_moves=None, estimated_prices=None, aux_ids=None) -> dict:
    """overrides: rc2f 교정 CSV(ledger_overrides*.csv) 경로 목록 — MANUAL_OVERRIDES 뒤에 같은 메커니즘으로 적용(ledger_repairs.apply_csv_overrides,
    pre = 평가 전 set/add/drop, post = 평가 뒤 status/isEstimate/값 재확정). eventId 로 지정된 행은 overrides_ref_ledger(rc2 원장)로 푼다.
    결과는 <out dir>/ca_ledger_csv_overrides.csv(overrideId, status applied/rejected, reason, affected_rows, eventIds)."""
    import ledger_repairs as rep

    t0 = time.time()
    csv_ovr, csv_rejected = rep.load_csv_overrides(overrides, overrides_ref_ledger, rep.load_split_moves(split_moves)) if overrides else ([], [])
    csv_results = {}
    for rj in csv_rejected:
        csv_results[rj["overrideId"]] = {"overrideId": rj["overrideId"], "caseId": "", "kind": "ledger", "action": "", "status": "rejected", "reason": rj["reason"], "affected_rows": 0, "target": ""}
    if overrides:
        log.info("csv overrides: %d loaded, %d rejected at load", len(csv_ovr), len(csv_rejected))
    root, stage = Path(root), Path(stage)
    work = stage / "work"
    work.mkdir(parents=True, exist_ok=True)
    out = out or (stage / "events" / "ca_ledger.parquet")
    inp = load_inputs(root, work, lineage_path, lifecycle_path)
    ids = sorted(inp["instruments"].tradingItemId.astype(int).tolist())
    if pull and any(inp[k] is None for k in ("ciqsplit", "ciqsplitcache", "divcache_noncash", "ciqcurrency")):
        pull_db(work, ids)
        inp = load_inputs(root, work, lineage_path, lifecycle_path)
    ccy = {int(a): str(b) for a, b in zip(inp["ciqcurrency"].currencyid, inp["ciqcurrency"].isocode)} if inp["ciqcurrency"] is not None else {USD: "USD"}
    cal = Calendar.from_root(root)
    id_set = set(ids)
    aux_ids = set(int(i) for i in aux_ids) if aux_ids else set()      # D1: 보조 종목 id 를 파일 없이 줄 수 있음(발행본은 aux 가격이 base_data 에 합쳐져 있음)
    if aux_base_data is not None and Path(aux_base_data).exists():
        aux_ids |= set(pq.read_table(aux_base_data, columns=["tradingItemId"]).column(0).unique().to_pylist())
    t1 = time.time()

    # 1) 현금(세전 규칙·rescinded 제외)
    cash, repairs = build_cash_rows(inp["dividends"], cal, ccy, rep.gross_first)
    # 2) 가격 lookup 대상 id(base ∪ aux)
    dist_stock = inp["distributions"][inp["distributions"].kind == "stock"]
    r3_rows, rep_rows = rep.load_r3_events(r3_events, inp["lineage"]) if r3_events else (pd.DataFrame(), [])
    repairs += rep_rows
    if len(r3_rows):
        al, mv = cal.align(r3_rows.exDate.to_numpy())
        r3_rows["notes"] = np.where(mv, r3_rows.notes.astype(str) + ";exDate_moved_from=" + r3_rows.exDate.dt.strftime("%Y-%m-%d"), r3_rows.notes.astype(str))
        r3_rows["exDate"] = al
    need = set(split_boundaries(inp["split"]).tradingItemId.astype(int)) | set(dist_stock.tradingItemId.astype(int)) \
        | set(dist_stock.newTradingItemId.dropna().astype(int)) | set(inp["events"].successorTradingItemId.dropna().astype(int)) \
        | set(inp["events"][inp["events"].hasCashLeg | inp["events"].hasStockLeg].tradingItemId.astype(int)) \
        | set(inp["dividends"][inp["dividends"].dividendTypeId.isin([20, 31])].tradingItemId.astype(int)) \
        | set(inp["lineage"].oldId.astype(int)) | set(inp["lineage"].newId.astype(int)) | rep.price_ids(root) \
        | set(cash[np.abs((cash.exDate - cash.tradingItemId.map(inp["lifecycle"].set_index("tradingItemId").lastTradeDate)).dt.days.fillna(999)) <= 5].tradingItemId.astype(int))
    if len(r3_rows):
        need |= set(r3_rows.tradingItemId.astype(int)) | set(int(x) for x in r3_rows.childTradingItemId.dropna())
    if csv_ovr:
        need |= rep.csv_override_ids(csv_ovr)
    need &= (id_set | aux_ids)
    closes = RawCloses([base_data, aux_base_data], need, cache=work / "anchor_closes.parquet")
    t2 = time.time()
    # 3) 분할·스핀·합병·계보
    split = build_split_rows(inp["split"], inp["ciqsplit"], cal, closes)
    repairs += split.attrs.get("repairs", [])
    if estimated_prices is not None and Path(estimated_prices).exists():
        split, n_est_px = mark_estimated_price_split_rows(split, pd.read_csv(estimated_prices, dtype=str, keep_default_na=False), closes)
        if n_est_px:
            log.info("split rows corroborated with ESTIMATED prices (rc2f price_overrides isEstimate) -> estimated: %d", n_est_px)
    split, rep_rows = rep.split_artifact_check(split, inp["ciqsplit"], inp["ciqsplitcache"], closes, root, cal)   # 97 인공물(rename 병합 전)
    repairs += rep_rows
    stock = build_stock_rows(inp["distributions"], inp["dividends"], cal, inp["lifecycle"], inp["lineage"], split_rows=split, r3_rows=r3_rows)
    dropped_all = list(stock.attrs.get("dropped", []))
    src_status = read_source_status(work / "source_status.json")
    ca_latest = (src_status.get("co_acthist") or {}).get("latest") if src_status else None
    n_ended_markers = n_marker_terms_in_ledger = 0
    merger = build_merger_rows(inp["events"], inp["lineage"], cal)
    if len(r3_rows):
        r3m = r3_rows[r3_rows.eventType.isin(MERGER_TYPES)].copy()
        if len(r3m):
            for c in merger.columns:
                if c not in r3m.columns:
                    r3m[c] = np.nan
            merger = pd.concat([merger, r3m[[c for c in merger.columns]].assign(_prio=0.5)], ignore_index=True)
        r3c = r3_rows[r3_rows.eventType.isin(["cash_dividend", "special_dividend"]) & (r3_rows.legType == "cash")].copy()
        if len(r3c):
            for c in cash.columns:
                if c not in r3c.columns:
                    r3c[c] = None
            cash = pd.concat([cash, r3c[[c for c in cash.columns]]], ignore_index=True)
    linrows = build_lineage_rows(inp["lineage"], cal, inp["lifecycle"])
    _, _, _, _, linrows, rep_rows = rep.apply_overrides(stock, merger, cash, split, inp, cal, lineage_rows=linrows, frames_only=("lineage",))
    repairs += rep_rows
    # 4) 같은 날 rename + split = 하나의 수량 전환(R2-2) — 귀속 환산에 최종 전환 비율이 필요하므로 먼저
    linrows, split, conflicts, rep_rows = merge_rename_with_split(linrows, split, closes)
    repairs += rep_rows
    # 5) 사건 귀속(F4): exDate 에 거래되던 id 로(전환일 leg 는 새 주 기준 환산; 수동 교정은 거래 id 기준으로 적으므로 귀속 뒤에 적용)
    stock, d1 = attribute_to_trading_id(stock, inp["lifecycle"], inp["lineage"], linrows, rc2_rules=rc2_rules, cal=cal)
    cash, d2 = attribute_to_trading_id(cash, inp["lifecycle"], inp["lineage"], linrows, rc2_rules=rc2_rules, cal=cal)
    # X3 (a): reassigned rows are mirrored back onto the original id only when it has closes around exDate (stitched history)
    n_copy_req = n_copy_kept = 0
    for d in (d1, d2):
        keep_d = []
        for x in d:
            if str(x["reason"]).startswith("history_reassigned_copy:"):
                n_copy_req += 1
                ex = pd.Timestamp(x["exDate"])
                cb, db_ = closes.before([x["tradingItemId"]], [ex])
                ca, da_ = closes.on_or_after([x["tradingItemId"]], [ex], REASSIGNED_COPY_WINDOW_DAYS)
                if np.isfinite(cb[0]) and (ex - pd.Timestamp(db_[0])).days <= REASSIGNED_COPY_WINDOW_DAYS and np.isfinite(ca[0]):
                    keep_d.append(x); n_copy_kept += 1
            else:
                keep_d.append(x)
        d[:] = keep_d
    if n_copy_req:
        log.info("reassigned rows mirrored onto the original id (X3 a): %d requested, %d with closes around exDate", n_copy_req, n_copy_kept)
    dropped_all += d1 + d2
    # 6) 교정(수동 override·TYC·GS 관측·워런트·VIA.B·IAC 2008): 평가 전에 적용해야 ① 로 평가됨
    stock, merger, cash, split, linrows, rep_rows = rep.apply_overrides(stock, merger, cash, split, inp, cal, lineage_rows=linrows, frames_only=("stock", "merger", "cash", "split"))
    repairs += rep_rows
    # 6b) rc2f 교정 CSV(pre: 평가 전 set/add/drop; MANUAL_OVERRIDES 와 같은 메커니즘)
    csv_exdate_moves = {}
    if csv_ovr:
        fr, rep_rows = rep.apply_csv_overrides({"stock": stock, "merger": merger, "cash": cash, "split": split, "lineage": linrows}, csv_ovr, "pre", csv_results)
        stock, merger, cash, split, linrows = fr["stock"], fr["merger"], fr["cash"], fr["split"], fr["lineage"]
        repairs += rep_rows
        # R-X0 2nd review P2-13: a keeper whose exDate the CSV moved — stitched copies dropped under the OLD date must still find it
        for o in csv_ovr:
            if o["action"] == "set" and csv_results.get(o["overrideId"], {}).get("status") == "applied" and not csv_results[o["overrideId"]].get("noop"):
                new_ex = pd.Timestamp(o["set"]["exDate"]) if "exDate" in o["set"] and pd.notna(o["set"]["exDate"]) else pd.Timestamp(o["exDate"])
                new_leg = o["set"].get("legType", o["legType"])
                if new_ex != pd.Timestamp(o["exDate"]) or new_leg != o["legType"]:      # keeper re-keyed (exDate and/or legType) -> copies follow (P2-13, L1 fix 5)
                    csv_exdate_moves[(int(o["tradingItemId"]), pd.Timestamp(o["exDate"]), o["legType"])] = (new_ex, new_leg)
        log.info("csv overrides (pre): %s", {k: sum(1 for r in csv_results.values() if r["status"] == k) for k in ("applied", "rejected")})
    # 6b') D4 인계: co_acthist 적재 정지 뒤 종료된 id 의 표시용 행(work/source_status.json 이 있을 때만; 전체·증분 같은 단계). 수동 교정·CSV add·
    #      R3·distributions 행이 모두 프레임에 있는 시점이라 원장 자체의 조건 행을 보고 거짓 마커를 피한다(R-D1 P2-1)
    if ca_latest:
        cand = ended_without_terms_markers(inp["lifecycle"], inp["events"], inp["lineage"], cal, pd.Timestamp(ca_latest), pd.Timestamp(cal.last),
                                           ledger_rows=pd.concat([f[["tradingItemId", "exDate", "eventType"]] for f in (stock, merger, cash) if f is not None and len(f)], ignore_index=True))
        n_marker_terms_in_ledger = int(cand.attrs.get("terms_in_ledger", 0))
        if len(cand):
            for c in stock.columns:
                if c not in cand.columns:
                    cand[c] = None if c in ("_r3_status",) else np.nan
            stock = pd.concat([stock, cand[stock.columns]], ignore_index=True)
            n_ended_markers = len(cand)
    # 6c) rc2f: 2차 합병 leg 의 exDate 를 정산 bar 로 정렬(R-X4 §4-2; rc2 규칙에서는 생략)
    if not rc2_rules:
        stock, rep_rows = align_legs_to_settlement_bar(stock, inp["lifecycle"], inp["events"], cal, closes)
        repairs += rep_rows
    # 6) 합병 현금 중복·정산성 배당(F1, R2-3)
    cash, merger, d3, rep_rows = reconcile_merger_cash_duplicates(cash, merger, inp["lifecycle"], closes)
    dropped_all += d3
    repairs += rep_rows
    # 7) 자회사 평가(P10, 사건 단위 ③④ 배분)
    stock = value_children(stock, closes, inp["divcache_noncash"], cash, split, id_set | aux_ids)
    repairs += stock.attrs.pop("repairs", [])
    stock, rep_rows = rep.apply_value_overrides(stock)          # R1c 리드 지시: 평가 뒤 수동 unresolved(값 NaN, 함의값 notes)
    repairs += rep_rows
    if n_ended_markers:
        m = stock.notes.astype(str).str.contains(ENDED_MARKER_TAG, regex=False) & ~stock.repairOf.astype(str).str.contains(rep.CSV_TAG + ":", regex=False)
        if m.any():                                                  # 표시용 행은 값 추정 없이 unresolved 로 유지(교정 CSV 가 조건을 준 행은 제외)
            stock.loc[m, "childValueSource"] = "unknown"
            stock.loc[m, "childValuePerOldShare"] = np.nan
            stock.loc[m, "status"] = "unresolved"
            stock.loc[m, "isEstimate"] = False
    merger_stock = merger[merger.legType == "stock"]
    merger_cash = merger[merger.legType == "cash"]
    merger_stock = value_children(merger_stock, closes, None, cash, split, id_set | aux_ids) if len(merger_stock) else merger_stock
    merger_stock.attrs.pop("repairs", None)
    # 8) 배당계수 스케일 오류 55 종목·분할 인공물 97 건 대조 → 교정 로그(+ 행 status/repairOf)
    cash, rep_rows = rep.divfactor_scale_repairs(cash, inp["dividend_factor"], closes, root, inp["lineage"])
    repairs += rep_rows
    split = reconcile_split_same_day(split, stock, cash, closes)
    # 9) F7 진단(계보 비율 가격 검증)
    ratio_diag = lineage_ratio_check(linrows, stock, cash, closes)
    # 10) 벤더 스텝만 있는 원장 공백 표시(R2-4)
    gap_path = work / "gap_candidates_union.csv"
    # R2 가 목록을 갱신해도 이전 후보를 잃지 않도록 파일들의 합집합(id, date)을 쓴다. 이전 빌드의 합집합 파일도 포함(누적): R2 는 원장에
    # 마커 행이 생긴 셀을 다음 목록에서 빼므로(공백 아님), 합집합이 누적이 아니면 재빌드마다 마커가 사라졌다 다시 생긴다(R1b)
    # the scratchpad R2 lists seed the union only when no union file exists yet (rc2 bootstrap); a daily chain carries the union (R-D2 P2-11)
    gap_files = [Path(g) for g in ([gap_candidates] if gap_candidates else []) + (R2_GAP_CANDIDATES if not gap_path.exists() else []) + [gap_path] if g and Path(g).exists()]
    gap_path = None
    if gap_files:
        frames = []
        for g in gap_files:
            c = pd.read_csv(g)
            c = c.rename(columns={"id": "tradingItemId"})
            dcol = next((k for k in ("vendorImpliedD", "vendor_implied_value", "impliedD") if k in c.columns), None)
            frames.append(pd.DataFrame({"tradingItemId": c.tradingItemId.astype(int), "date": pd.to_datetime(c.date), "vendorImpliedD": c[dcol].astype(float) if dcol else np.nan,
                                        "prevDate": pd.to_datetime(c.prevDate) if "prevDate" in c.columns else pd.NaT}))
        allc = pd.concat(frames, ignore_index=True).sort_values(["tradingItemId", "date", "prevDate"], na_position="last").drop_duplicates(["tradingItemId", "date"], keep="first")
        gap_path = work / "gap_candidates_union.csv"
        allc.to_csv(gap_path, index=False)
        log.info("vendor-step gap candidates: %s -> union %d", [str(g) for g in gap_files], len(allc))
    gaps = vendor_gap_markers(gap_path, inp["dividends"], cal, inp["lifecycle"], cash, split, stock, rc2_rules=rc2_rules, closes=closes)
    gap_counts = gaps.attrs.get("counts", {}) if len(gaps) else {}
    if not rc2_rules and gap_path is not None and "candidates" in gaps.attrs and "prevDateSource" in gaps.attrs["candidates"].columns:
        gaps.attrs["candidates"].to_csv(gap_path, index=False)        # prevDate filled from raw closes -> ledger_qa Q10 sees the cell's own interval
    # 10b) rc2f 교정 CSV(post: 평가·교정 규칙 뒤 status/isEstimate/값 재확정, pre 에서 못 찾은 행은 최종 프레임(gaps 포함)에서 재시도)
    if csv_ovr:
        gattrs = dict(gaps.attrs) if len(gaps) else {}
        fr, rep_rows = rep.apply_csv_overrides({"cash": cash, "split": split, "stock": stock, "merger_stock": merger_stock, "merger_cash": merger_cash,
                                                "lineage": linrows, "gaps": gaps}, csv_ovr, "post", csv_results, dropped=dropped_all)
        cash, split, stock, merger_stock, merger_cash, linrows, gaps = fr["cash"], fr["split"], fr["stock"], fr["merger_stock"], fr["merger_cash"], fr["lineage"], fr["gaps"]
        if len(gaps):
            gaps.attrs.update(gattrs)
        repairs += rep_rows
        log.info("csv overrides (post): %s", {k: sum(1 for r in csv_results.values() if r["status"] == k) for k in ("applied", "rejected")})
    t3 = time.time()
    if csv_ovr:
        dropped_all += [r for res in csv_results.values() for r in res.get("dropped_rows", [])]     # CSV drop 도 드롭 파일에(Q4 dividendId 회계)
    # 11) 조립·저장
    ledger = finalize([cash, split, stock, merger_stock, merger_cash, linrows, gaps])
    dropped_dup = ledger.attrs.pop("dropped")          # attrs 는 parquet 메타데이터로 직렬화되므로 파일 쓰기 전에 비운다
    n_dup = ledger.attrs.pop("n_duplicates_dropped", 0)
    info_file = write_ledger(ledger, out)
    n_rep = write_repairs(repairs, out.parent / "ca_ledger_repairs.csv")
    dropped_df = pd.concat([pd.DataFrame(dropped_all), dropped_dup], ignore_index=True) if dropped_all else dropped_dup
    stitched = stitched_copies_map(dropped_df, ledger, csv_exdate_moves)
    # 리드 결정: 이어붙인 과거의 복사본은 stitched_copy 행으로 원장에 남긴다(export 제외). 드롭 파일에서는 뺀다.
    copies = make_stitched_copies(ledger, stitched)
    if len(copies):
        ledger = pd.concat([ledger, copies], ignore_index=True).sort_values(["tradingItemId", "exDate", "legType", "childTradingItemId"], kind="stable").reset_index(drop=True)
        if not ledger.eventId.is_unique:
            raise ValueError("stitched copies produced duplicate eventIds")
        info_file = write_ledger(ledger, out)
    copied = set(zip(copies.tradingItemId.astype(int), pd.to_datetime(copies.exDate), copies.legType, copies.keeperEventId)) if len(copies) else set()
    stitched["copyEventId"] = [ledger.eventId[(ledger.tradingItemId == int(t)) & (ledger.exDate == pd.Timestamp(d)) & (ledger.keeperEventId == k)].iloc[0]
                               if (int(t), pd.Timestamp(d), l, k) in copied else "" for t, d, l, k in zip(stitched.tradingItemId, stitched.exDate, stitched.legType, stitched.keeperEventId.fillna("").astype(str))]
    # L1 integration (X0 fix 1): a CSV `add` that replicates a stitched history copy (X3 mechanism A rows, added in the pre phase before the copies
    # exist) collides with the copy on the contract key -> same amount (+-1e-6): the CSV row is dropped and the override becomes a no-op
    # (duplicate_of_existing); the copy (exportExclude, keeper on the trading id) is the row that stays. Different amount: both stay (Q1 lists them).
    if csv_ovr and len(copies):
        ledger, dup_records = drop_csv_adds_duplicating_copies(ledger, csv_ovr, csv_results)
        if dup_records:
            dropped_all += dup_records
            info_file = write_ledger(ledger, out)
    # R1c ④: 전환일 leg 를 이어붙인 승계 라인에 미러(stitched_copy, exportExclude)
    tcopies, tmap = make_transition_copies(ledger, closes, inp["lifecycle"])
    pd.DataFrame(tmap.attrs.get("skipped", []), columns=["keeperEventId", "successor", "reason"]).to_csv(out.parent / "ca_ledger_transition_copies_skipped.csv", index=False)
    if len(tcopies):
        ledger = pd.concat([ledger, tcopies], ignore_index=True).sort_values(["tradingItemId", "exDate", "legType", "childTradingItemId"], kind="stable").reset_index(drop=True)
        if not ledger.eventId.is_unique:
            raise ValueError("transition copies produced duplicate eventIds")
        info_file = write_ledger(ledger, out)
        tmap2 = tmap.copy()
        for c in stitched.columns:
            if c not in tmap2.columns:
                tmap2[c] = ""
        stitched = pd.concat([stitched, tmap2[stitched.columns]], ignore_index=True)
    # X3 (a): a reassigned-copy request whose keeper row no longer exists (moved row deduplicated later) is not a drop of anything -> not listed
    if len(stitched):
        ghost = stitched[(stitched.reason.astype(str).str.startswith("history_reassigned_copy")) & (stitched.keeperEventId.fillna("").astype(str) == "")]
        if len(ghost):
            gk = set(zip(ghost.tradingItemId.astype(int), pd.to_datetime(ghost.exDate), ghost.legType))
            stitched = stitched[~stitched.index.isin(ghost.index)]
            keep = ~pd.Series([(int(t), pd.Timestamp(d), l) in gk and str(r).startswith("history_reassigned_copy") for t, d, l, r in zip(dropped_df.tradingItemId, dropped_df.exDate, dropped_df.legType, dropped_df.reason)], index=dropped_df.index)
            dropped_df = dropped_df[keep]
            log.info("reassigned-copy requests without a keeper row removed: %d", len(ghost))
    stitched.to_csv(out.parent / "ca_ledger_stitched_copies.csv", index=False)
    if overrides:
        tag_ids = ledger.repairOf.map(lambda x: [t.split(":", 1)[1] for t in x.split(";") if t.startswith(rep.CSV_TAG + ":")] if isinstance(x, str) else [])
        by_oid = {}
        for eid, oids, is_copy_row in zip(ledger.eventId, tag_ids, ledger.source == "stitched_copy"):
            for oid in oids:
                by_oid.setdefault(oid, []).append(eid)
        rows = []
        for oid, r in csv_results.items():
            r = dict(r)
            r["eventIds"] = r.get("eventIds_override") or ";".join(by_oid.get(oid, []))
            rows.append(r)
        pd.DataFrame([{k: v for k, v in r.items() if k not in ("dropped_rows", "noop", "pre_set", "eventIds_override")} for r in rows], columns=["overrideId", "caseId", "kind", "action", "status", "reason", "affected_rows", "target", "eventIds"]).to_csv(
            out.parent / "ca_ledger_csv_overrides.csv", index=False)
        n_bad = sum(1 for r in csv_results.values() if r["status"] != "applied")
        log.info("csv overrides: %d applied, %d rejected -> %s", len(csv_results) - n_bad, n_bad, out.parent / "ca_ledger_csv_overrides.csv")
    is_copy = dropped_df.reason.astype(str).str.split(":").str[0].isin(STITCHED_COPY_REASONS) if len(dropped_df) else pd.Series(dtype=bool)
    dropped_df = dropped_df[~is_copy] if len(dropped_df) else dropped_df
    dropped_df.to_csv(out.parent / "ca_ledger_dropped_duplicates.csv", index=False)
    if len(ratio_diag):
        ratio_diag.to_csv(out.parent / "ca_ledger_lineage_ratio_check.csv", index=False)
    pd.DataFrame(conflicts).to_csv(out.parent / "ca_ledger_lineage_split_conflicts.csv", index=False)
    # 진단 부속 파일(원장 계약 밖): 자회사 base_data 부재 목록(R3), 유니버스 플래그
    missing_child = ledger[(ledger.legType.isin(["stock", "rights"])) & ledger.childTradingItemId.notna()
                           & ~ledger.childTradingItemId.astype("Int64").isin(list(id_set | aux_ids))]
    missing_child[["eventId", "tradingItemId", "exDate", "eventType", "childTradingItemId", "childRatio", "status"]].to_csv(
        out.parent / "ca_ledger_child_not_in_base_data.csv", index=False)
    uf = universe_flags(ledger, inp["membership"])
    uf.assign(eventId=ledger.eventId.values, eventType=ledger.eventType.values, status=ledger.status.values).to_parquet(
        out.parent / "ca_ledger_universe_flags.parquet", index=False)
    info = {
        "out": str(out), "file": info_file, "repairs_rows": n_rep, "duplicates_dropped_key_collision": int(n_dup),
        "dropped_rows_total": int(len(dropped_df)), "dropped_by_reason": dropped_df.reason.str.split(":").str[0].value_counts().to_dict() if len(dropped_df) else {},
        "rows_by_type": ledger.eventType.value_counts().to_dict(), "rows_by_status": ledger[ledger.source != "stitched_copy"].status.value_counts().to_dict(),
        "stitched_copy_rows": int((ledger.source == "stitched_copy").sum()), "stitched_copy_status": ledger[ledger.source == "stitched_copy"].status.value_counts().to_dict(),
        "transition_copy_rows": int(len(tcopies)), "transition_copies_skipped_series_differs": int(len(tmap.attrs.get("skipped", []))),
        "rows_by_leg": ledger.legType.value_counts().to_dict(), "child_not_in_base_data": int(len(missing_child)),
        "child_ids_not_in_base_data": sorted(set(missing_child.childTradingItemId.astype(int).tolist())),
        "lineage_ratio_check_rows": int(len(ratio_diag)), "lineage_split_conflicts": len(conflicts), "vendor_gap_markers": int(len(gaps)),
        "vendor_only_steps": gap_counts, "stitched_copies_map_rows": int(len(stitched)),
        "r3_handover_rows": int(len(r3_rows)), "aux_ids_available": len(aux_ids),
        "ended_without_terms_markers": int(n_ended_markers), "ended_markers_skipped_terms_in_ledger": int(n_marker_terms_in_ledger), "source_status": src_status or None,
        "rules": "rc2" if rc2_rules else "rc2f(X3 a/b/c)", "reassigned_copies_kept": n_copy_kept,
        "csv_overrides": {"files": [str(p) for p in (overrides or [])], "loaded": len(csv_ovr), "applied": sum(1 for r in csv_results.values() if r["status"] == "applied"),
                          "rejected": sum(1 for r in csv_results.values() if r["status"] != "applied")},
        "seconds": {"inputs": round(t1 - t0, 1), "closes": round(t2 - t1, 1), "legs_repairs": round(t3 - t2, 1),
                    "write": round(time.time() - t3, 1), "total": round(time.time() - t0, 1)},
        "peak_rss_gb": round(rss_gb(), 2), "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "asOf": str(pd.Timestamp(cal.last).date()), "builtAt": dt.datetime.now().isoformat(timespec="seconds"),
        "inputs": {"root": str(root), "base_data": str(base_data), "aux_base_data": str(aux_base_data), "lineage": inp["lineage_source"],
                   "lifecycle": inp["lifecycle_source"], "r3_events": str(r3_events), "gap_candidates": str(gap_path)},
    }
    (out.parent / "ca_ledger_build_info.json").write_text(json.dumps(info, indent=1, default=str))
    log.info("ledger: %d rows -> %s (%.0fs)", len(ledger), out, info["seconds"]["total"])
    return info


# --------------------------------------------------------------------------------------
# self-test(합성 입력): 같은 날 현금+주식 두 행, 분할 경계 재현, P10 ① 우선, 휴장일 이동
# --------------------------------------------------------------------------------------
def self_test() -> list[dict]:
    import tempfile
    import ledger_repairs as rep

    res = []
    T = pd.Timestamp
    cal = Calendar(pd.bdate_range("2020-01-02", "2020-12-31").to_numpy().astype("datetime64[D]"))
    # 휴장일 이동: 2020-01-04(토) → 01-06(월); 캘린더 밖(2030-03-02 토) → 03-04(월)
    al, mv = cal.align(pd.to_datetime(["2020-01-04", "2020-01-06", "2030-03-02"]))
    res.append({"case": "calendar align: Sat→Mon inside, Sat→Mon outside(weekday rule), weekday unchanged",
                "expected": ["2020-01-06", "2020-01-06", "2030-03-04"], "got": [str(pd.Timestamp(x).date()) for x in al],
                "ok": [str(pd.Timestamp(x).date()) for x in al] == ["2020-01-06", "2020-01-06", "2030-03-04"] and mv.tolist() == [True, False, True]})
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        dates = pd.bdate_range("2020-01-02", "2020-03-31")
        rows = []
        k_ex = dates.get_loc(T("2020-02-03"))
        v_spin = 0.5 * np.float32(40.0 * (1 + 0.001 * k_ex))         # 같은 날 스핀(자회사 2, 비율 0.5) 가치/구주 = 20.44 → 부모 가격이 그만큼 빠진다(R1c ② 정합)
        for tid, p0 in ((1, 100.0), (2, 40.0)):
            for k, d in enumerate(dates):
                px = p0 * (1 + 0.001 * k)
                if tid == 1 and d >= T("2020-02-03"):
                    px = (px - float(v_spin)) / 2                   # 2:1 분할 2020-02-03 + 스핀 가치 차감
                rows.append((d, tid, np.float32(px)))
        base = pd.DataFrame(rows, columns=["date", "tradingItemId", "close"])
        pq.write_table(pa.Table.from_pandas(base.assign(tradingItemId=base.tradingItemId.astype(np.int64)), preserve_index=False), tmp / "base.parquet")
        closes = RawCloses(tmp / "base.parquet")
        got = closes.at([1, 1, 2], pd.to_datetime(["2020-02-03", "2020-02-01", "2020-01-02"]))
        res.append({"case": "RawCloses.at exact-date only (Saturday → NaN)", "expected": [">0", "NaN", 40.0],
                    "got": [round(float(got[0]), 4), got[1], float(got[2])], "ok": bool(np.isfinite(got[0]) and np.isnan(got[1]) and abs(got[2] - 40) < 1e-6)})
        pb, db_ = closes.before([1], pd.to_datetime(["2020-02-03"]))
        res.append({"case": "RawCloses.before = last close strictly before (2020-01-31)", "expected": "2020-01-31",
                    "got": _d(db_[0]), "ok": _d(db_[0]) == "2020-01-31" and pb[0] == closes.at([1], [T("2020-01-31")])[0]})
        split = pd.DataFrame({"tradingItemId": [1, 1, 2], "validFrom": pd.to_datetime(["1900-01-01", "2020-02-03", "1900-01-01"]),
                              "validTo": pd.to_datetime(["2020-02-03", None, None]), "factor": [2.0, 1.0, 1.0],
                              "source": ["synthetic_pre_first", "ciqsplitcache", "synthetic_no_window"], "snapshotDate": T("2026-09-17")})
        ciqsplit = pd.DataFrame({"splitid": [7], "tradingitemid": [1], "exdate": [T("2020-02-03")], "rate": [2.0], "splittypeid": [12], "appliedflag": [1]})
        sp = build_split_rows(split, ciqsplit, cal, closes)
        res.append({"case": "split row from factor boundary: parentFactor 2.0, ciqsplit matched, raw jump ok", "expected": [1, 2.0, "ok", "split"],
                    "got": [len(sp), float(sp.parentFactor[0]), sp.status[0], sp.eventType[0]],
                    "ok": len(sp) == 1 and sp.parentFactor[0] == 2.0 and sp.status[0] == "ok" and sp.eventType[0] == "split"})
        # 분할 없는 계수 스텝(가격 안 움직임) → unresolved
        split_bad = split.copy()
        split_bad.loc[1, "validFrom"] = T("2020-03-02")
        split_bad.loc[0, "validTo"] = T("2020-03-02")
        sp_bad = build_split_rows(split_bad, ciqsplit.iloc[:0], cal, closes)
        res.append({"case": "negative: factor step without price move and without ciqsplit → unresolved", "expected": "unresolved",
                    "got": sp_bad.status[0], "ok": sp_bad.status[0] == "unresolved"})
        # 현금 + 같은 날 스핀(자회사 2, 비율 0.5, exDate 2020-02-03 종가 40×1.021) → 두 행, ① 평가
        dv = pd.DataFrame({"dividendId": [11, 12], "tradingItemId": [1, 1], "exDate": [T("2020-02-03"), T("2020-02-03")],
                           "payDate": [T("2020-02-20"), pd.NaT], "recordDate": [pd.NaT, pd.NaT], "announcedDate": [pd.NaT, pd.NaT],
                           "divAmount": [1.0, np.nan], "netAmount": [np.nan, np.nan], "grossAmount": [np.nan, np.nan],
                           "currencyId": [160.0, 160.0], "fxRate": [1.0, 1.0], "dividendTypeId": [1, 20],
                           "dividendTypeName": ["No special code applies", "Stock dividend in stock of another issue"],
                           "supplementalTypeName": ["Regular cash dividend", None], "isCash": [True, False], "isStock": [False, True]})
        cash, _ = build_cash_rows(dv, cal, {160: "USD"}, rep.gross_first)
        dist = pd.DataFrame([{"tradingItemId": 1, "exDate": T("2020-02-03"), "kind": "stock", "leg": 0, "newTradingItemId": 2,
                              "ratioNewPerOld": 0.5, "eventClass": "spinoff", "status": "ok", "source": "ciqdividend20", "evidence": "TEST",
                              "newIdMethod": "test", "mismatchClass": None, "ratioBasis": "per_share_text", "cashEquivalentText": 20.0,
                              "impliedValueFracExCash": np.nan}])
        lc = pd.DataFrame({"tradingItemId": [1, 2], "firstTradeDate": [T("2020-01-02")] * 2, "lastTradeDate": [pd.NaT, pd.NaT]})
        ln = pd.DataFrame(columns=["oldId", "newId", "relation", "effectiveDate", "ratio", "evidence", "confidence"])
        st = build_stock_rows(dist, dv, cal, lc, ln)
        st = value_children(st, closes, None, cash, sp, {1, 2})
        c2 = float(closes.at([2], [T("2020-02-03")])[0])
        led = finalize([cash, sp, st])
        same_day = led[(led.tradingItemId == 1) & (led.exDate == T("2020-02-03"))]
        res.append({"case": "same-day cash + split + spin → 3 rows; spin valued by child close (P10 ①, cash-equivalent ignored)",
                    "expected": {"rows": 3, "legs": ["cash", "quantity", "stock"], "value": round(0.5 * c2, 6), "src": "child_close_ex"},
                    "got": {"rows": len(same_day), "legs": sorted(same_day.legType), "value": round(float(st.childValuePerOldShare[0]), 6), "src": st.childValueSource[0]},
                    "ok": len(same_day) == 3 and sorted(same_day.legType) == ["cash", "quantity", "stock"] and abs(st.childValuePerOldShare[0] - 0.5 * c2) < 1e-9 and st.childValueSource[0] == "child_close_ex"})
        res.append({"case": "eventId format <id>_<yyyymmdd>_<type>_<seq> and unique", "expected": "1_20200203_cash_dividend_0",
                    "got": led.eventId.iloc[0], "ok": led.eventId.is_unique and "1_20200203_cash_dividend_0" in set(led.eventId)})
        # 자회사 종가 없음 + cash equivalent → event_value(estimated)
        dist2 = dist.copy(); dist2["newTradingItemId"] = 3
        st2 = value_children(build_stock_rows(dist2, dv.iloc[:0], cal, lc, ln), closes, None, cash, sp, {1, 2})
        res.append({"case": "child without price → P10 ③ event_value (estimated)", "expected": ["event_value", "estimated", 20.0],
                    "got": [st2.childValueSource[0], st2.status[0], float(st2.childValuePerOldShare[0])],
                    "ok": st2.childValueSource[0] == "event_value" and st2.status[0] == "estimated" and st2.childValuePerOldShare[0] == 20.0})
        # R1c ②: 부모 가격이 빠지지 않은 날의 큰 ① 값(가치 20% vs 하락 0%) → 창 안에 맞는 날이 없으면 unresolved(표시), 있으면 exDate 교정
        rows_flat = pd.DataFrame(rows, columns=["date", "tradingItemId", "close"])
        flat = rows_flat[rows_flat.tradingItemId == 2]._append(pd.DataFrame({"date": dates, "tradingItemId": 1, "close": np.float32(100.0)}))
        pq.write_table(pa.Table.from_pandas(flat.assign(tradingItemId=flat.tradingItemId.astype(np.int64)), preserve_index=False), tmp / "flat.parquet")
        st4 = value_children(build_stock_rows(dist, dv.iloc[:0], cal, lc, ln), RawCloses(tmp / "flat.parquet"), None, cash.iloc[:0], sp.iloc[:0], {1, 2})
        res.append({"case": "R1c ②: ① value 20% of price but parent flat → unresolved (implausible, no matching date)", "expected": ["unknown", "unresolved"],
                    "got": [st4.childValueSource[0], st4.status[0]], "ok": st4.childValueSource[0] == "unknown" and st4.status[0] == "unresolved"
                    and "implausible_child_close_ex" in st4.notes[0]})
        shifted = rows_flat.copy()                                   # 분할(02-03)은 제자리, 분배 하락만 하루 뒤(02-04) 관측 → exDate 02-04 교정
        m1 = (shifted.tradingItemId == 1) & (shifted.date == T("2020-02-03"))
        shifted.loc[m1, "close"] = np.float32(100.0 * (1 + 0.001 * k_ex) / 2)      # 02-03 = 분할만 반영(분배 전 수준)
        pq.write_table(pa.Table.from_pandas(shifted.assign(tradingItemId=shifted.tradingItemId.astype(np.int64)), preserve_index=False), tmp / "shift.parquet")
        st5 = value_children(build_stock_rows(dist, dv.iloc[:0], cal, lc, ln), RawCloses(tmp / "shift.parquet"), None, cash.iloc[:0], sp, {1, 2})
        c2b = float(RawCloses(tmp / "shift.parquet").at([2], [T("2020-02-04")])[0])
        # 교정된 날(02-04) 직전 보유 주식은 02-03 분할(pf 2) 뒤의 신주 → 비율 0.5/2 = 0.25, 값 = 0.25 × 자회사 02-04 종가
        res.append({"case": "R1c ②: parent drop observed one day later (after the split) → exDate corrected to 02-04, ratio re-based 0.5→0.25, value = 0.25 × child close",
                    "expected": ["2020-02-04", round(0.25 * c2b, 6), 0.25, "child_close_ex"],
                    "got": [_d(st5.exDate[0]), round(float(st5.childValuePerOldShare[0]), 6), float(st5.childRatio[0]), st5.childValueSource[0]],
                    "ok": _d(st5.exDate[0]) == "2020-02-04" and abs(st5.childValuePerOldShare[0] - 0.25 * c2b) < 1e-9 and abs(st5.childRatio[0] - 0.25) < 1e-12
                    and "exDate_corrected_from=2020-02-03" in st5.notes[0]})
        dist3 = dist2.copy(); dist3["cashEquivalentText"] = np.nan; dist3["ratioNewPerOld"] = np.nan
        st3 = value_children(build_stock_rows(dist3, dv.iloc[:0], cal, lc, ln), closes, None, cash, sp, {1, 2})
        res.append({"case": "no ratio, no value source → unresolved with NaN value (visible, not hidden)", "expected": ["unknown", "unresolved"],
                    "got": [st3.childValueSource[0], st3.status[0]],
                    "ok": st3.childValueSource[0] == "unknown" and st3.status[0] == "unresolved" and np.isnan(st3.childValuePerOldShare[0])})
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="rc1 발행본 또는 로컬 복사본(dividends/, distributions/, factors/, events/, master/, universe/, calendar.parquet)")
    ap.add_argument("--stage", type=Path, default=DEFAULT_STAGE)
    ap.add_argument("--base-data", type=Path, default=DEFAULT_BASE)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--no-db", action="store_true", help="xfeed 조회 없이 work 캐시만 사용")
    ap.add_argument("--lineage", type=Path, default=R3_STAGE / "master" / "lineage.parquet", help="lineage v2(R3) — 없으면 root/master/lineage.parquet")
    ap.add_argument("--lifecycle", type=Path, default=R3_STAGE / "master" / "lifecycle.parquet", help="lifecycle(R3 패치본) — 없으면 root/master")
    ap.add_argument("--aux-base-data", type=Path, default=R3_STAGE / "base_data_aux.parquet", help="R3 보조 종목 가격(자회사 평가용)")
    ap.add_argument("--r3-events", type=Path, default=R3_STAGE / "for_R1_events.csv", help="R3 인계 사건 leg")
    ap.add_argument("--gap-candidates", type=Path, default=None, help="R2 ledger_gap_candidates.csv(벤더 스텝만 있는 셀)")
    ap.add_argument("--overrides", type=Path, nargs="*", default=None, help="rc2f ledger_overrides*.csv (orch_rc2f/BRIEF.md §3; MANUAL_OVERRIDES 뒤에 적용)")
    ap.add_argument("--overrides-ref-ledger", type=Path, default=SCRATCH / "build_rc2_final" / "events" / "ca_ledger.parquet",
                    help="eventId 로 지정된 교정을 (id, exDate, legType, child) 로 푸는 참조 원장(rc2)")
    ap.add_argument("--rc2-rules", action="store_true", help="rc2 규칙 그대로(X3 인계 (a) reassigned 복사본·(b) 마커 창·(c) 전환일 분기 비활성) — 비트 동일 게이트용")
    ap.add_argument("--split-moves", type=Path, default=None, help="rc2f_apply 의 split_boundary_moves.csv: 같은 실행의 split move_date 로 옮긴 경계 → eventId 로 지정된 ledger set/drop 대상을 새 날짜로 재매핑")
    ap.add_argument("--estimated-prices", type=Path, default=None, help="rc2f_apply 의 price_override_estimates.csv(추정 가격 교정 창): 그 가격으로 corroborate 된 split 행은 estimated (R-Y2 P1 ①)")
    ap.add_argument("--closes-max-date", default=None, help="D1 게이트 시뮬레이션: 이 날짜 뒤의 raw 종가를 읽지 않음(앵커 캐시 미사용)")
    ap.add_argument("--threads", type=int, default=8, help="pyarrow CPU/IO 스레드 상한(공유 머신 규칙 ≤ 8)")
    ap.add_argument("--self-test", action="store_true")
    import ledger_incremental
    ledger_incremental.add_incremental_args(ap)
    args = ap.parse_args(argv)
    args._given = {t.split("=", 1)[0] for t in (argv if argv is not None else sys.argv[1:]) if isinstance(t, str) and t.startswith("--")}   # 명시된 옵션(증분 모드의 기본값 해석용)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    pa.set_cpu_count(max(1, min(int(args.threads), 8)))
    pa.set_io_thread_count(max(1, min(int(args.threads), 8)))
    if args.closes_max_date:
        global CLOSES_MAX_DATE
        CLOSES_MAX_DATE = pd.Timestamp(args.closes_max_date)
    if args.incremental:
        return ledger_incremental.main_incremental(args)
    if args.self_test:
        res = self_test()
        for r in res:
            print(("OK  " if r["ok"] else "BAD ") + r["case"], "| expected", r["expected"], "| got", r["got"])
        n_bad = sum(not r["ok"] for r in res)
        print(f"self-test: {len(res) - n_bad}/{len(res)} cases behave as expected")
        return 1 if n_bad else 0
    info = build(args.root, args.stage, args.base_data, args.out, pull=not args.no_db, lineage_path=args.lineage, lifecycle_path=args.lifecycle,
                 aux_base_data=args.aux_base_data, r3_events=args.r3_events, gap_candidates=args.gap_candidates,
                 overrides=args.overrides, overrides_ref_ledger=args.overrides_ref_ledger, rc2_rules=args.rc2_rules, split_moves=args.split_moves,
                 estimated_prices=args.estimated_prices)
    print(json.dumps({k: v for k, v in info.items() if k != "child_ids_not_in_base_data"}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
