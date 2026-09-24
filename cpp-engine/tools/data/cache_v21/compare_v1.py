#!/home/jgo/usr/venv-3.11/bin/python
"""cache_v2.1 vs 팀 v1 캐시 일별 비교 리포트 (T10, agent-compare).

v1(`/home/base/cache/cache_v1_cash_1d`, 읽기 전용; wide parquet, 열 = str(tradingItemId), index `__index_level_0__`)와
v2.1 빌드(long + 구간 계수, 참조 로더 `loader.CacheV21`)를 비교해 `<out>/compare_YYYY-MM-DD.md` 와 표(csv/parquet)를 만든다.
매일 돌릴 수 있게 설계: 전 기간 모드(--since 이후 전체)와 일별 모드(--asof 기준 최근 N 거래일)를 한 번에 산출.

비교 항목
  1. 종목 집합: v1 close 열 vs v2.1 종목(유니버스 멤버-일 가중), v1 마지막 종가일 vs lifecycle.lastTradeDate, v1 0채움 규칙 확인
  2. 종가(분할조정): v1 BaseData/close vs v2.1 close split_adj(lifecycle cut) 상대차 분포·연도별·종목별·원인 분류
  3. 수익률: v1 Returns/HoldingReturn_Usd vs v2.1 returns_total = (a) 분할/정정 차이 + (b) 배당·스핀오프 효과 분해
  4. 유니버스: v1 TopUniverseV1 vs v2.1 membership(id 수준·라인 수준), 차이 종목·원인
  5. 포트폴리오 프록시: Top500 tradable∧universe 동일가중 롱 북(일별 리밸런스) 수익률 차이(연도별, 표본수 병기)
  6. 일별 모드: 최근 N 거래일 종가·수익률·유니버스 차이 건수

규칙: v1·/home/base·/home/jgo/61 은 읽기만, DB 접근 없음, 스레드 ≤ 8, 그림 없음(표만).

사용
  compare_v1.py --v1-root /home/base/cache/cache_v1_cash_1d --v21-root <build> --out <dir> \
      [--v1-snapshot-root <snap>] [--since 2010-01-01] [--universe Top500] [--asof 2026-09-15] [--mode both] [--daily-days 5]
  compare_v1.py selftest --v21-root <build> --out <dir>     # 반례 self-test(합성 v1 에 심은 차이를 정확히 검출하는지)
"""
from __future__ import annotations

import argparse
import json
import re
import resource
import sys
import time
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))
from loader import CacheV21  # noqa: E402

NS_PER_DAY = 86_400_000_000_000
UNIVERSES = ("Top500", "Top1000", "ALL")
CLOSE_THRESHOLDS = (1e-6, 1e-4, 1e-2, 5e-2)
RET_THRESHOLDS = (1e-6, 1e-4, 1e-3, 1e-2, 1e-1)
V1_MASTER_DEFAULT = Path("/home/base/vendor_data/uss/cash/master/uss_cash_master_acc.parquet")
MIADJ_OVERWRITE_SINCE = pd.Timestamp("2025-06-01")   # v1 daily_uss_stock_split.py: 이 날 이후 분할 종목은 열 전체를 miadjprice 로 덮어씀


def log(msg: str) -> None:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024
    print(f"[{time.strftime('%H:%M:%S')}] [rss {rss:.1f}GB] {msg}", flush=True)


# ----------------------------------------------------------------------------------------------------------
# 표 유틸
# ----------------------------------------------------------------------------------------------------------
PLAIN_INT_COLS = ("tradingItemId", "lineEndId", "oldId", "newId", "exchangeId", "qualityFlag", "연도", "year", "companyId")


def fmt(v, col: str = "") -> str:
    if v is None:
        return ""
    if isinstance(v, (bool, np.bool_)):
        return str(bool(v))
    if isinstance(v, (int, np.integer)):
        return str(int(v)) if (col in PLAIN_INT_COLS or col.endswith("Id")) else f"{int(v):,}"
    if isinstance(v, (float, np.floating)):
        if not np.isfinite(v):
            return ""
        a = abs(v)
        if a == 0:
            return "0"
        if a >= 100:
            return f"{v:,.1f}"
        if a >= 1:
            return f"{v:.4f}"
        if a >= 1e-3:
            return f"{v:.5f}"
        return f"{v:.3e}"
    if isinstance(v, (pd.Timestamp, np.datetime64)):
        t = pd.Timestamp(v)
        return "" if pd.isna(t) else t.strftime("%Y-%m-%d")
    s = str(v)
    return s.replace("|", "\\|").replace("\n", " ")


def md_table(df: pd.DataFrame, index: bool = False, max_rows: int | None = None) -> str:
    if df is None or len(df) == 0:
        return "(없음)\n"
    d = df.copy()
    if index:
        d = d.reset_index()
    if max_rows is not None and len(d) > max_rows:
        d = d.head(max_rows)
    cols = [str(c) for c in d.columns]
    lines = ["| " + " | ".join(c.replace("|", "\\|") for c in cols) + " |", "|" + "---|" * len(cols)]
    for row in d.itertuples(index=False):
        lines.append("| " + " | ".join(fmt(v, c) for v, c in zip(row, cols)) + " |")
    return "\n".join(lines) + "\n"


def save_table(df: pd.DataFrame, out_dir: Path, name: str, parquet: bool = False) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    if parquet:
        df.to_parquet(out_dir / f"{name}.parquet", index=False)
    else:
        df.to_csv(out_dir / f"{name}.csv", index=False)


def _pct(n: int, d: int) -> float:
    return float(n) / float(d) if d else float("nan")


# ----------------------------------------------------------------------------------------------------------
# v1 wide 읽기
# ----------------------------------------------------------------------------------------------------------
def read_v1_index(path: Path) -> np.ndarray:
    return pq.ParquetFile(path).read(columns=["__index_level_0__"]).column(0).to_numpy()


def read_v1_wide(path: Path, since: pd.Timestamp, asof: pd.Timestamp | None = None):
    """v1 wide parquet → (dates DatetimeIndex, ids int64[N], values 2D). since 이후 행만(1 row group 파일이라 전체 읽고 슬라이스)."""
    pf = pq.ParquetFile(path)
    idx = pf.read(columns=["__index_level_0__"]).column(0).to_numpy()
    lo = int(np.searchsorted(idx, np.datetime64(since, "ns")))
    hi = int(np.searchsorted(idx, np.datetime64(asof, "ns"), side="right")) if asof is not None else len(idx)
    tbl = pf.read(use_threads=True)
    sl = tbl.slice(lo, hi - lo)
    del tbl
    df = sl.to_pandas()
    del sl
    ids = np.array([int(c) for c in df.columns], dtype=np.int64)
    dates = pd.DatetimeIndex(df.index)
    vals = df.to_numpy()
    del df
    return dates, ids, vals


class V1Data:
    """v1 캐시(2010+ 슬라이스). close·HoldingReturn·TopUniverseV1 마스크. 열 축 = 파일 순서(ids), 행 = dates."""

    def __init__(self, root: Path, since: pd.Timestamp, asof: pd.Timestamp | None, universes=UNIVERSES, load_returns=True,
                 load_universe=True):
        self.root = Path(root)
        t0 = time.perf_counter()
        self.dates, self.ids, self.close = read_v1_wide(self.root / "BaseData" / "close.parquet", since, asof)
        self.close = self.close.astype(np.float64, copy=False)
        log(f"v1 close {self.close.shape} read {time.perf_counter() - t0:.1f}s from {self.root}")
        self.hr = None
        self.univ: dict[str, np.ndarray] = {}
        self.axis_notes: list[str] = []
        if load_returns:
            d, i, hr = read_v1_wide(self.root / "Returns" / "HoldingReturn_Usd.parquet", since, asof)
            self.hr = self._align(d, i, hr.astype(np.float64, copy=False), np.nan, "Returns/HoldingReturn_Usd")
        if load_universe:
            for u in universes:
                p = self.root / "TopUniverseV1" / f"{u}.parquet"
                if not p.exists():
                    warnings.warn(f"v1 universe file missing: {p}")
                    continue
                d, i, m = read_v1_wide(p, since, asof)
                self.univ[u] = self._align(d, i, m.astype(bool, copy=False), False, f"TopUniverseV1/{u}")
        self.tokens_to_remove = self._read_tokens()
        log(f"v1 loaded: {len(self.dates)} dates × {len(self.ids)} ids, universes {list(self.univ)}")

    def _align(self, d: pd.DatetimeIndex, i: np.ndarray, vals: np.ndarray, fill, label: str) -> np.ndarray:
        """다른 v1 파일을 close 축(dates × ids)에 맞춘다. v1 파일들은 행·열이 하루/한 열씩 어긋날 수 있음(예: Top1000 11,812행 vs Top500 11,811행)."""
        same_d = len(d) == len(self.dates) and np.array_equal(d.values, self.dates.values)
        same_i = len(i) == len(self.ids) and np.array_equal(i, self.ids)
        if same_d and same_i:
            return vals
        r = d.get_indexer(self.dates)
        c = pd.Index(i).get_indexer(self.ids)
        out = np.full((len(self.dates), len(self.ids)), fill, dtype=vals.dtype)
        ok_r, ok_c = r >= 0, c >= 0
        out[np.ix_(ok_r, ok_c)] = vals[np.ix_(r[ok_r], c[ok_c])]
        note = (f"v1 {label}: axis differs from close (dates {len(d)} vs {len(self.dates)}, missing in file {int((~ok_r).sum())}, "
                f"extra in file {int(len(d) - ok_r.sum())}; ids {len(i)} vs {len(self.ids)}, missing {int((~ok_c).sum())}) → reindexed to close axis")
        self.axis_notes.append(note)
        log(note)
        return out

    def _read_tokens(self) -> set[int]:
        p = self.root / "TopUniverseV1" / "METADATA.yml"
        if not p.exists():
            return set()
        txt = p.read_text()
        m = re.search(r"TokensToRemove:\s*\n((?:\s*-\s*'?\d+'?\s*\n?)+)", txt)
        if not m:
            return set()
        return {int(x) for x in re.findall(r"\d+", m.group(1))}


def read_v1_master(path: Path | None) -> pd.DataFrame | None:
    if path is None or not Path(path).exists():
        return None
    names = pq.ParquetFile(path).schema_arrow.names
    cols = [c for c in ["tradingItemId", "symbol", "companyName", "exchangeId", "securityEndDate", "currentPrimary"] if c in names]
    t = pq.read_table(path, columns=cols).to_pandas()
    if "tradingItemId" in t.columns:
        t = t.set_index("tradingItemId")
    t.index = t.index.astype(np.int64)
    t.index.name = "tradingItemId"
    t = t[~t.index.duplicated(keep="last")]
    return t


# ----------------------------------------------------------------------------------------------------------
# v2.1 읽기
# ----------------------------------------------------------------------------------------------------------
class V21Data:
    def __init__(self, root: Path, master_root: Path | None, since: pd.Timestamp, asof: pd.Timestamp | None, threads: int = 8,
                 universes=UNIVERSES, ids: np.ndarray | None = None):
        self.root = Path(root)
        self.master_root = Path(master_root) if master_root else self.root
        t0 = time.perf_counter()
        if ids is None:
            ids = np.sort(pq.read_table(self.master_root / "master" / "instruments.parquet",
                                        columns=["tradingItemId"]).column(0).to_numpy().astype(np.int64))
        # ids 를 명시해야 start 로 잘린 범위에 행이 없는 id 가 returns_total 의 strict id 검사에 걸리지 않는다.
        self.cv = CacheV21(self.root, master_root=self.master_root, start=since, end=asof, threads=threads, ids=ids)
        self.dates = self.cv.dates()
        self.ids = self.cv.ids()
        self.close = self.cv.get_panel("close", "split_adj", as_array=True).astype(np.float64)                  # lifecycle cut
        self.close_uncut = self.cv.get_panel("close", "split_adj", as_array=True, cut=None).astype(np.float64)
        self.qflag = self.cv.get_panel("qualityFlag", "raw", as_array=True, cut=None)
        self.ret = self.cv.get_returns(as_array=True, cut="lifecycle")
        self.trad = self.cv.get_mask("tradable", as_array=True)
        self.univ: dict[str, np.ndarray] = {}
        self.univ_v1rule: dict[str, np.ndarray] = {}
        for u in universes:
            self.univ[u] = self.cv.get_mask(f"universe:{u}", as_array=True)
            try:
                self.univ_v1rule[u] = self.cv.get_mask(f"universe_v1rule:{u}", as_array=True)
            except (FileNotFoundError, KeyError):
                pass
        self.lifecycle = self.cv.lifecycle()
        self.lineage = self.cv.lineage()
        self.current = pq.read_table(self.master_root / "master" / "current.parquet").to_pandas().set_index("tradingItemId")
        self.instruments = pq.read_table(self.master_root / "master" / "instruments.parquet").to_pandas().set_index("tradingItemId")
        self.split = pq.read_table(self.root / "factors" / "split.parquet").to_pandas()
        self.dividends = self._read_optional(self.root / "dividends" / "dividends_long.parquet",
                                             ["tradingItemId", "exDate", "divAmount_USD", "dividendTypeId", "dividendTypeName", "isCash", "isStock"])
        self.distributions = self._read_optional(self.root / "distributions" / "distributions.parquet",
                                                 ["tradingItemId", "exDate", "kind", "status", "cashPerShareUSD", "newSymbol", "ratioNewPerOld",
                                                  "impliedValueFrac"])
        self.manifest = json.loads((self.root / "manifest.json").read_text()) if (self.root / "manifest.json").exists() else {}
        self.line_end = self._build_line_map()
        self.miadj_overwrite_ids = self._miadj_overwrite_ids()
        self._split_by_id = {int(k): g.to_numpy(np.float64) for k, g in self.split.groupby("tradingItemId")["factor"]}
        self._ratio_cache: dict[int, np.ndarray] = {}
        log(f"v2.1 loaded: {len(self.dates)} dates × {len(self.ids)} ids in {time.perf_counter() - t0:.1f}s "
            f"(timings {dict((k, round(v, 2)) for k, v in self.cv.timings.items())})")

    @staticmethod
    def _read_optional(p: Path, cols):
        if not p.exists():
            return None
        names = pq.ParquetFile(p).schema_arrow.names
        return pq.read_table(p, columns=[c for c in cols if c in names]).to_pandas()

    def _build_line_map(self) -> dict[int, int]:
        """lineage relist/rename(high/medium) 체인: id → 라인 끝 id. 체인에 없는 id 는 자기 자신."""
        ln = self.lineage
        ln = ln[ln["relation"].isin(["relist", "rename"]) & ln["confidence"].isin(["high", "medium"])]
        nxt = {}
        for o, n in zip(ln["oldId"].to_numpy(np.int64), ln["newId"].to_numpy(np.int64)):
            if o == n:
                continue
            # 같은 old 에 복수 new 가 있으면 effectiveDate 가 늦은 쪽(마지막 행)을 따른다
            nxt[int(o)] = int(n)
        end = {}
        for o in nxt:
            cur, seen = o, {o}
            while cur in nxt and nxt[cur] not in seen:
                cur = nxt[cur]
                seen.add(cur)
            end[o] = cur
        return end

    def _miadj_overwrite_ids(self) -> set[int]:
        sp = self.split
        m = (sp["validFrom"] >= MIADJ_OVERWRITE_SINCE) & sp["source"].isin(["ciqsplitcache", "ciqpriceequity_adjf"])
        return set(sp.loc[m, "tradingItemId"].astype(np.int64).tolist())

    def line_of(self, ids: np.ndarray) -> np.ndarray:
        return np.array([self.line_end.get(int(i), int(i)) for i in ids], dtype=np.int64)

    def symbol(self, i: int) -> str:
        try:
            return str(self.current.at[int(i), "symbol"])
        except KeyError:
            return ""

    def name(self, i: int) -> str:
        try:
            return str(self.instruments.at[int(i), "companyName"])[:40]
        except KeyError:
            return ""

    def split_ratios(self, i: int) -> np.ndarray:
        """종목의 분할계수 값 ∪ 계수 비(f_i/f_j), 1 근처(±0.5%) 제외. v1 옛 기준 셀은 v1/v2.1 비율이 이 집합의 원소."""
        i = int(i)
        if i in self._ratio_cache:
            return self._ratio_cache[i]
        f = self._split_by_id.get(i, np.empty(0))
        f = np.unique(f[np.isfinite(f) & (f > 0)])
        if f.size == 0:
            r = np.empty(0)
        else:
            r = np.unique(np.concatenate([f, (f[:, None] / f[None, :]).ravel()]))
            r = r[np.abs(r - 1.0) > 5e-3]
        self._ratio_cache[i] = r
        return r


# ----------------------------------------------------------------------------------------------------------
# 정렬(공통 축)
# ----------------------------------------------------------------------------------------------------------
class Aligned:
    """공통 날짜 × 공통 id(둘 중 한쪽이라도 since 이후 종가가 있는 id) 로 정렬한 배열 묶음."""

    def __init__(self, v1: V1Data, v2: V21Data, asof: pd.Timestamp | None):
        d1 = v1.dates[np.isfinite(v1.close).any(axis=1)]           # v1 all-NaN 행(당일 Delay 행) 제외
        dates = d1.intersection(v2.dates)
        if asof is not None:
            dates = dates[dates <= asof]
        self.dates = dates
        r1 = v1.dates.get_indexer(dates)
        r2 = v2.dates.get_indexer(dates)
        v1_has = np.isfinite(v1.close[r1]).any(axis=0)
        v2_has = np.isfinite(v2.close_uncut[r2]).any(axis=0)
        self.v1_ids_valid = v1.ids[v1_has]
        self.v2_ids_valid = v2.ids[v2_has]
        common = np.intersect1d(v1.ids, v2.ids)
        keep = np.isin(common, self.v1_ids_valid) | np.isin(common, self.v2_ids_valid)
        self.ids = common[keep]
        c1 = pd.Index(v1.ids).get_indexer(self.ids)
        c2 = pd.Index(v2.ids).get_indexer(self.ids)
        assert (c1 >= 0).all() and (c2 >= 0).all()
        self.v1_close = v1.close[np.ix_(r1, c1)]
        self.v1_hr = v1.hr[np.ix_(r1, c1)] if v1.hr is not None else None
        self.v1_univ = {u: m[np.ix_(r1, c1)] for u, m in v1.univ.items()}
        self.v2_close = v2.close[np.ix_(r2, c2)]
        self.v2_close_uncut = v2.close_uncut[np.ix_(r2, c2)]
        self.v2_qflag = v2.qflag[np.ix_(r2, c2)]
        self.v2_ret = v2.ret[np.ix_(r2, c2)]
        self.v2_trad = v2.trad[np.ix_(r2, c2)]
        self.v2_univ = {u: m[np.ix_(r2, c2)] for u, m in v2.univ.items()}
        self.r1, self.r2, self.c1, self.c2 = r1, r2, c1, c2
        self.years = np.asarray(self.dates.year)
        log(f"aligned: {len(self.dates)} dates × {len(self.ids)} common ids "
            f"(v1 valid ids {len(self.v1_ids_valid)}, v2.1 valid ids {len(self.v2_ids_valid)})")

    @staticmethod
    def prev_obs_idx(arr: np.ndarray) -> np.ndarray:
        """각 셀의 직전 관측(유한) 행 인덱스(없으면 -1): 행 t 에 대해 max{s < t : arr[s] finite}."""
        T = arr.shape[0]
        rows = np.where(np.isfinite(arr), np.arange(T, dtype=np.int64)[:, None], -1)
        cm = np.maximum.accumulate(rows, axis=0)
        out = np.empty_like(cm)
        out[0] = -1
        out[1:] = cm[:-1]
        return out

    @staticmethod
    def prev_obs_value(arr: np.ndarray, prev_idx: np.ndarray) -> np.ndarray:
        cols = np.broadcast_to(np.arange(arr.shape[1]), arr.shape)
        out = arr[np.maximum(prev_idx, 0), cols]
        out = np.where(prev_idx >= 0, out, np.nan)
        return out

    def price_return_v21(self) -> np.ndarray:
        """v2.1 가격만 수익률: close split_adj(cut) 의 직전 관측 행 대비. 첫 행 NaN(범위 밖 이전 행 미사용)."""
        c = self.v2_close
        prev = self.prev_obs_value(c, self.prev_obs_idx(c))
        out = c / prev - 1.0
        out[~np.isfinite(c)] = np.nan
        return out


# ----------------------------------------------------------------------------------------------------------
# 1. 종목 집합
# ----------------------------------------------------------------------------------------------------------
def section_ids(v1: V1Data, v2: V21Data, al: Aligned, v1m: pd.DataFrame | None, out: Path, top: int) -> tuple[str, dict]:
    md = ["## 1. 종목 집합\n"]
    summ = {}
    v1_valid = set(al.v1_ids_valid.tolist())
    v2_valid = set(al.v2_ids_valid.tolist())
    both = v1_valid & v2_valid
    v1_only = v1_valid - set(v2.ids.tolist())
    v1_only_nopx = (v1_valid & set(v2.ids.tolist())) - v2_valid     # v2.1 종목이지만 기간 내 종가 없음
    v2_only = v2_valid - set(v1.ids.tolist())
    v2_only_nopx = (v2_valid & set(v1.ids.tolist())) - v1_valid

    # 유니버스 멤버-일 가중(각 캐시 자기 유니버스 기준)
    def member_days_v1(ids, u):
        if u not in v1.univ:
            return 0
        cols = pd.Index(v1.ids).get_indexer(np.array(sorted(ids), dtype=np.int64))
        cols = cols[cols >= 0]
        return int(v1.univ[u][np.ix_(al.r1, cols)].sum()) if cols.size else 0

    def member_days_v2(ids, u):
        cols = pd.Index(v2.ids).get_indexer(np.array(sorted(ids), dtype=np.int64))
        cols = cols[cols >= 0]
        return int(v2.univ[u][np.ix_(al.r2, cols)].sum()) if cols.size else 0

    rows = [
        {"집합": "v1 종가 보유 id(since 이후)", "id 수": len(v1_valid)},
        {"집합": "v2.1 종가 보유 id(since 이후, cut 전)", "id 수": len(v2_valid)},
        {"집합": "공통(둘 다 종가)", "id 수": len(both)},
        {"집합": "v1에만(v2.1 종목 집합 밖)", "id 수": len(v1_only),
         **{f"v1 {u} 멤버-일": member_days_v1(v1_only, u) for u in v1.univ}},
        {"집합": "v1에만 종가(v2.1 종목이나 기간 내 종가 없음)", "id 수": len(v1_only_nopx),
         **{f"v1 {u} 멤버-일": member_days_v1(v1_only_nopx, u) for u in v1.univ}},
        {"집합": "v2.1에만(v1 열 없음)", "id 수": len(v2_only),
         **{f"v2.1 {u} 멤버-일": member_days_v2(v2_only, u) for u in v2.univ}},
        {"집합": "v2.1에만 종가(v1 열 있으나 기간 내 종가 없음)", "id 수": len(v2_only_nopx),
         **{f"v2.1 {u} 멤버-일": member_days_v2(v2_only_nopx, u) for u in v2.univ}},
    ]
    t = pd.DataFrame(rows)
    for c in t.columns:
        if c.endswith("멤버-일"):
            t[c] = t[c].astype("Int64")
    md.append(md_table(t))
    summ.update({"v1_valid_ids": len(v1_valid), "v21_valid_ids": len(v2_valid), "common_ids": len(both),
                 "v1_only_ids": len(v1_only), "v21_only_ids": len(v2_only)})

    # v1에만 / v2.1에만 상위 목록(멤버-일 순)
    def top_list(ids, side):
        recs = []
        for i in ids:
            rec = {"tradingItemId": i}
            if side == "v1":
                c = int(np.flatnonzero(v1.ids == i)[0])
                col = v1.close[al.r1, c]
                fin = np.isfinite(col)
                rec.update({"symbol": (v1m.at[i, "symbol"] if v1m is not None and i in v1m.index else ""),
                            "companyName": (str(v1m.at[i, "companyName"])[:40] if v1m is not None and i in v1m.index else ""),
                            "exchangeId": (v1m.at[i, "exchangeId"] if v1m is not None and i in v1m.index else ""),
                            "v1 첫 종가": al.dates[fin][0] if fin.any() else pd.NaT, "v1 마지막 종가": al.dates[fin][-1] if fin.any() else pd.NaT,
                            "v1 종가 일수": int(fin.sum()),
                            **{f"v1 {u} 멤버-일": int(v1.univ[u][al.r1, c].sum()) for u in v1.univ},
                            "inV21Master": i in v2.instruments.index})
            else:
                c = int(np.flatnonzero(v2.ids == i)[0])
                col = v2.close_uncut[al.r2, c]
                fin = np.isfinite(col)
                ins = v2.instruments.loc[i] if i in v2.instruments.index else None
                rec.update({"symbol": v2.symbol(i), "companyName": v2.name(i),
                            "exchangeId": v2.current.at[i, "exchangeId"] if i in v2.current.index else "",
                            "inV1Rule": bool(ins["inV1Rule"]) if ins is not None else None,
                            "inV1Cache": bool(ins["inV1Cache"]) if ins is not None else None,
                            "firstTradeDate": v2.lifecycle.at[i, "firstTradeDate"] if i in v2.lifecycle.index else pd.NaT,
                            "lastTradeDate": v2.lifecycle.at[i, "lastTradeDate"] if i in v2.lifecycle.index else pd.NaT,
                            "v2.1 종가 일수": int(fin.sum()),
                            **{f"v2.1 {u} 멤버-일": int(v2.univ[u][al.r2, c].sum()) for u in v2.univ}})
            recs.append(rec)
        df = pd.DataFrame(recs)
        if len(df):
            keys = [c for c in df.columns if c.endswith("Top500 멤버-일")] + [c for c in df.columns if c.endswith("ALL 멤버-일")] + \
                   [c for c in df.columns if c.endswith("종가 일수")]
            df = df.sort_values(keys, ascending=False)
        return df

    t_v1only = top_list(sorted(v1_only | v1_only_nopx), "v1")
    t_v2only = top_list(sorted(v2_only | v2_only_nopx), "v2")
    save_table(t_v1only, out, "ids_v1_only")
    save_table(t_v2only, out, "ids_v21_only")
    md.append(f"\n### 1-1. v1에만 있는 종목 상위 {top}(v1 Top500·ALL 멤버-일 순; 전체 `ids_v1_only.csv`)\n")
    md.append(md_table(t_v1only, max_rows=top))
    rule_ct = t_v2only["inV1Rule"].value_counts(dropna=False).to_dict() if len(t_v2only) else {}
    md.append(f"\n### 1-2. v2.1에만 있는 종목 상위 {top}(v2.1 Top500·ALL 멤버-일 순; 전체 `ids_v21_only.csv`). inV1Rule 분포 {rule_ct}\n")
    md.append(md_table(t_v2only, max_rows=top))

    # v1 마지막 종가일 vs lifecycle.lastTradeDate
    T = len(al.dates)
    fin1 = np.isfinite(al.v1_close)
    last1 = np.where(fin1.any(axis=0), T - 1 - np.argmax(fin1[::-1], axis=0), -1)
    lc = v2.lifecycle.reindex(al.ids)
    ltd = pd.to_datetime(lc["lastTradeDate"]).values
    v1_last = np.where(last1 >= 0, al.dates.values[np.maximum(last1, 0)], np.datetime64("NaT"))
    has = (last1 >= 0) & ~pd.isna(ltd)
    ltd_capped = np.minimum(ltd[has], al.dates.values[-1])         # 진행 중 종목은 마지막 공통일로 캡
    diff_days = ((v1_last[has] - ltd_capped) / np.timedelta64(1, "D")).astype(np.int64)
    bins = [-np.inf, -30.5, -5.5, -0.5, 0.5, 1.5, 5.5, 30.5, np.inf]
    labels = ["≤ −31 (v1 먼저 끊김)", "−30 ~ −6", "−5 ~ −1", "0 (같음)", "+1 (v1 하루 더)", "+2 ~ +5", "+6 ~ +30", "≥ +31 (v1 꼬리)"]
    cat = pd.Series(pd.cut(diff_days, bins=bins, labels=labels))
    vc = cat.value_counts().reindex(labels, fill_value=0)
    has_idx = np.flatnonzero(has)
    mem500 = al.v1_univ["Top500"][:, has_idx].sum(axis=0) if "Top500" in al.v1_univ else np.zeros(has_idx.size, dtype=int)
    memall = al.v1_univ["ALL"][:, has_idx].sum(axis=0) if "ALL" in al.v1_univ else np.zeros(has_idx.size, dtype=int)
    t_last = pd.DataFrame({"v1 마지막 종가일 − lastTradeDate(일)": labels, "종목 수": vc.to_numpy().astype(int)})
    t_last["비율"] = t_last["종목 수"] / max(int(has.sum()), 1)
    t_last["그중 v1 Top500 멤버 이력 종목"] = [int(((cat == lab).to_numpy() & (mem500 > 0)).sum()) for lab in labels]
    t_last["그중 v1 ALL 멤버 이력 종목"] = [int(((cat == lab).to_numpy() & (memall > 0)).sum()) for lab in labels]
    md.append(f"\n### 1-3. v1 마지막 종가일 − v2.1 lifecycle.lastTradeDate (공통 종목 {int(has.sum()):,}; lastTradeDate 는 마지막 공통일 {fmt(al.dates[-1])} 로 캡)\n")
    md.append(md_table(t_last))
    # 사례: v1 이 늦은(v1 꼬리) 상위, v1 이 빠른 상위
    ids_has = al.ids[has]
    ex = pd.DataFrame({"tradingItemId": ids_has, "v1 마지막 종가": v1_last[has], "lastTradeDate": ltd[has], "차이(일)": diff_days,
                       "endReason": lc.loc[ids_has, "endReason"].to_numpy(), "status": lc.loc[ids_has, "status"].to_numpy(),
                       "lastTradeSource": lc.loc[ids_has, "lastTradeSource"].to_numpy()})
    ex["symbol"] = [v2.symbol(i) for i in ex["tradingItemId"]]
    ex["companyName"] = [v2.name(i) for i in ex["tradingItemId"]]
    ex["v1 Top500 멤버-일"] = [int(al.v1_univ["Top500"][:, k].sum()) if "Top500" in al.v1_univ else 0 for k in np.flatnonzero(has)]
    ex = ex[ex["차이(일)"] != 0].sort_values("차이(일)", key=np.abs, ascending=False)
    save_table(ex, out, "last_close_vs_lastTradeDate")
    md.append(f"\n상위 {top}(|차이| 순; 전체 `last_close_vs_lastTradeDate.csv`). 양수 = v1 이 lastTradeDate 뒤에도 종가 보유(v1 꼬리), 음수 = v1 종가가 먼저 끊김.\n")
    md.append(md_table(ex, max_rows=top))
    summ["last_close_equal_frac"] = _pct(int((diff_days == 0).sum()), int(has.sum()))
    summ["last_close_v1_later_gt5d"] = int((diff_days > 5).sum())
    summ["last_close_v1_earlier_gt5d"] = int((diff_days < -5).sum())

    # v1 0채움 규칙 확인: v1 마지막 종가일 이후 HoldingReturn 값 분포
    if al.v1_hr is not None:
        after = np.arange(T)[:, None] > last1[None, :]
        after &= last1[None, :] >= 0
        hr_after = al.v1_hr[after]
        n_after = hr_after.size
        n_zero = int((hr_after == 0).sum())
        n_nan = int(np.isnan(hr_after).sum())
        n_other = n_after - n_zero - n_nan
        # 상장 전(첫 종가 이전)
        first1 = np.where(fin1.any(axis=0), np.argmax(fin1, axis=0), T)
        before = np.arange(T)[:, None] < first1[None, :]
        hr_before = al.v1_hr[before]
        t0 = pd.DataFrame([
            {"구간": "v1 마지막 종가일 이후", "셀": n_after, "hr == 0": n_zero, "hr NaN": n_nan, "그 외": n_other},
            {"구간": "v1 첫 종가일 이전(since 이후 범위 내)", "셀": hr_before.size, "hr == 0": int((hr_before == 0).sum()),
             "hr NaN": int(np.isnan(hr_before).sum()), "그 외": int(hr_before.size - (hr_before == 0).sum() - np.isnan(hr_before).sum())},
        ])
        md.append("\n### 1-4. v1 HoldingReturn 0채움 규칙 확인(공통 종목)\n")
        md.append(md_table(t0))
        # close NaN 인 셀(상장 중 결측)에서의 hr
        mid = ~fin1 & ~after & ~before
        hr_mid = al.v1_hr[mid]
        md.append(f"상장 구간 내 close NaN 셀 {hr_mid.size:,}: hr==0 {int((hr_mid == 0).sum()):,} · NaN {int(np.isnan(hr_mid).sum()):,} · "
                  f"그 외 {int(hr_mid.size - (hr_mid == 0).sum() - np.isnan(hr_mid).sum()):,} → close notna 셀만 유효로 취급.\n")
        summ["v1_hr_zero_fill_frac_after_last"] = _pct(n_zero, n_after)
    return "\n".join(md), summ


# ----------------------------------------------------------------------------------------------------------
# 2. 종가
# ----------------------------------------------------------------------------------------------------------
def classify_close_cells(al: Aligned, v2: V21Data, rows: np.ndarray, cols: np.ndarray, ratio: np.ndarray, rel: np.ndarray) -> np.ndarray:
    """종가 차이 셀 원인 분류(id 별 CIQ 분할비 대조 + 상수 비율 run 검출).

    v1 close 원천(리드 확인): 일별 append 는 ciqpriceequity(수집 당일 기준), 2025-06-01 이후 분할 종목은 열 전체를 miadjprice 로 재수집,
    2025-02-06 일괄 적재 이력은 원천 코드 미확인(상수비 차이가 miadjprice 원장과 정합하는 정황). 재수집 스크립트 = `daily_uss_stock_split.py`.
    v2.1 은 ciqpriceequity raw ÷ CIQ splitFactor.
      qflag2_ciq_mi_raw_mismatch : v2.1 qualityFlag 2 (ciq·mi raw 종가 0.1%↑ 차이 셀) → 원천 가격 자체가 다름
      stale_split_basis          : v1/v2.1 비율 = 그 종목 CIQ 분할계수(또는 계수 비) & 재수집 안 된 종목 → 수집 뒤 분할을 v1 이 미반영(옛 기준 셀)
      mi_repull_split_ledger_diff: 2025-06+ 분할로 miadjprice 재수집된 종목인데 비율이 CIQ 분할비 → MI 와 CIQ 분할 원장이 다름
      constant_ratio_run         : 그 종목 차이 셀의 60%↑가 한 비율(±0.2%) 을 공유(≥20셀) & CIQ 분할비 아님 → 조정 원장 차이(권리락·주식배당·MI≠CIQ)
      small_revision(<5%) / other_large(≥5%) : 그 외(벤더 정정·잠정가·반올림). 재수집 종목은 '(mi_repull)' 접미
    """
    n = rows.size
    cause = np.full(n, "", dtype=object)
    ids = al.ids[cols]
    qf = al.v2_qflag[rows, cols]
    repull = np.array([int(i) in v2.miadj_overwrite_ids for i in ids]) if n else np.zeros(0, dtype=bool)
    split_match = np.zeros(n, dtype=bool)
    const_run = np.zeros(n, dtype=bool)
    for i in np.unique(ids):
        sel = np.flatnonzero(ids == i)
        r = ratio[sel]
        R = v2.split_ratios(int(i))
        if R.size:
            split_match[sel] = np.min(np.abs(r[:, None] / R[None, :] - 1.0), axis=1) < 2e-3
        if sel.size >= 20:
            med = np.median(r)
            near = np.abs(r / med - 1.0) < 2e-3
            if near.mean() >= 0.6:
                const_run[sel[near]] = True
    suffix = np.where(repull, "(mi_repull)", "")
    cause[:] = np.where(rel < 5e-2, "small_revision(<5%)", "other_large(>=5%)")
    cause = np.char.add(cause.astype(str), suffix.astype(str)).astype(object)
    cause[const_run & ~split_match] = np.char.add("constant_ratio_run", suffix[const_run & ~split_match].astype(str))
    cause[split_match & ~repull] = "stale_split_basis"
    cause[split_match & repull] = "mi_repull_split_ledger_diff"
    cause[qf == 2] = "qflag2_ciq_mi_raw_mismatch"
    return cause


def close_compare(al: Aligned, v1_close: np.ndarray, v2: V21Data, out: Path, tag: str, top: int, list_cells: bool = True) -> tuple[str, dict]:
    md = []
    summ = {}
    both = np.isfinite(v1_close) & np.isfinite(al.v2_close)
    n_both = int(both.sum())
    rel = np.full(al.v2_close.shape, np.nan)
    rel[both] = np.abs(v1_close[both] - al.v2_close[both]) / np.abs(al.v2_close[both])
    rows = []
    for th in CLOSE_THRESHOLDS:
        n = int((rel[both] > th).sum())
        rows.append({"임계(상대차)": f"> {th:g}", "셀 수": n, "비율": _pct(n, n_both)})
    md.append(f"공통 유효 셀 {n_both:,} (v1 close notna ∧ v2.1 close split_adj cut notna). 상대차 = |v1 − v2.1| / |v2.1|.\n")
    md.append(md_table(pd.DataFrame(rows)))
    summ.update({f"close_frac_gt_{th:g}": _pct(int((rel[both] > th).sum()), n_both) for th in CLOSE_THRESHOLDS})
    summ["close_common_cells"] = n_both
    # 한쪽만 있는 셀
    v1_tail = np.isfinite(v1_close) & ~np.isfinite(al.v2_close) & np.isfinite(al.v2_close_uncut)
    v1_only = np.isfinite(v1_close) & ~np.isfinite(al.v2_close_uncut)
    v2_only = ~np.isfinite(v1_close) & np.isfinite(al.v2_close)
    md.append("\n한쪽만 종가가 있는 셀:\n")
    md.append(md_table(pd.DataFrame([
        {"구분": "v1 종가 ∧ v2.1 lifecycle 절단(꼬리: v2.1 은 lastTradeDate 뒤 값 보유)", "셀": int(v1_tail.sum()), "종목": int(v1_tail.any(axis=0).sum())},
        {"구분": "v1 종가 ∧ v2.1 행 없음", "셀": int(v1_only.sum()), "종목": int(v1_only.any(axis=0).sum())},
        {"구분": "v2.1 종가 ∧ v1 NaN", "셀": int(v2_only.sum()), "종목": int(v2_only.any(axis=0).sum())},
        {"구분": "  그중 v2.1 tradable(firstTradeDate~lastTradeDate) 셀 — 나머지는 이어붙인 과거(비거래 구간)", "셀": int((v2_only & al.v2_trad).sum()),
         "종목": int((v2_only & al.v2_trad).any(axis=0).sum())},
    ])))
    summ["close_v21_only_tradable_cells"] = int((v2_only & al.v2_trad).sum())
    summ.update({"close_v1_tail_cells": int(v1_tail.sum()), "close_v1_only_cells": int(v1_only.sum()), "close_v21_only_cells": int(v2_only.sum())})
    for arr, name, label in ((v1_only, "v1_only", "v1 종가 ∧ v2.1 행 없음"), (v2_only, "v21_only", "v2.1 종가 ∧ v1 NaN")):
        k = np.flatnonzero(arr.any(axis=0))
        if k.size == 0:
            continue
        lc_ = v2.lifecycle.reindex(al.ids[k])
        t_one = pd.DataFrame({"tradingItemId": al.ids[k], "symbol": [v2.symbol(i) for i in al.ids[k]], "companyName": [v2.name(i) for i in al.ids[k]],
                              "셀": arr[:, k].sum(axis=0), "첫 날": [al.dates[np.argmax(arr[:, j])] for j in k],
                              "마지막 날": [al.dates[len(al.dates) - 1 - np.argmax(arr[::-1, j])] for j in k],
                              "firstTradeDate": lc_["firstTradeDate"].to_numpy(), "lastTradeDate": lc_["lastTradeDate"].to_numpy(),
                              "v2.1 tradable 셀": [int((al.v2_trad[:, j] & arr[:, j]).sum()) for j in k],
                              "v1 Top500 멤버-일(해당 셀)": [int((al.v1_univ["Top500"][:, j] & arr[:, j]).sum()) if "Top500" in al.v1_univ else 0 for j in k],
                              "v2.1 Top500 멤버-일(해당 셀)": [int((al.v2_univ["Top500"][:, j] & arr[:, j]).sum()) for j in k]})
        t_one = t_one.sort_values("셀", ascending=False)
        save_table(t_one, out, f"close_{name}_cells_by_id_{tag}")
        md.append(f"\n{label} 상위 15 종목(전체 `close_{name}_cells_by_id_{tag}.csv`):\n")
        md.append(md_table(t_one, max_rows=15))

    # 연도별
    yrs = al.years
    recs = []
    for y in np.unique(yrs):
        m = yrs == y
        b = both[m]
        r = rel[m][b]
        recs.append({"연도": int(y), "공통 셀": int(b.sum()), **{f">{th:g}": _pct(int((r > th).sum()), int(b.sum())) for th in CLOSE_THRESHOLDS},
                     "v1 꼬리 셀": int(v1_tail[m].sum()), "v1만": int(v1_only[m].sum()), "v2.1만": int(v2_only[m].sum())})
    t_year = pd.DataFrame(recs)
    save_table(t_year, out, f"close_diff_by_year_{tag}")
    md.append("\n연도별 초과 비율:\n")
    md.append(md_table(t_year))

    # 셀 목록(>1e-4) + 원인
    sig = both & (rel > 1e-4)
    rr, cc = np.nonzero(sig)
    ratio = v1_close[rr, cc] / al.v2_close[rr, cc]
    cause = classify_close_cells(al, v2, rr, cc, ratio, rel[rr, cc]) if rr.size else np.empty(0, dtype=object)
    cells = pd.DataFrame({"date": al.dates.values[rr], "tradingItemId": al.ids[cc], "v1_close": v1_close[rr, cc], "v21_close_sadj": al.v2_close[rr, cc],
                          "ratio_v1_over_v21": ratio, "rel_diff": rel[rr, cc], "qualityFlag": al.v2_qflag[rr, cc], "cause": cause})
    if list_cells:
        save_table(cells, out, f"close_diff_cells_{tag}", parquet=True)
    t_cause = cells.groupby("cause").agg(셀=("date", "size"), 종목=("tradingItemId", "nunique"),
                                          중앙_상대차=("rel_diff", "median"), 최대_상대차=("rel_diff", "max")).reset_index().sort_values("셀", ascending=False)
    md.append(f"\n상대차 > 1e-4 셀 {len(cells):,} 원인 분류(`close_diff_cells_{tag}.parquet`). v1 close 원천: 일별 append = ciqpriceequity(수집 당일 기준), "
              "2025-06+ 분할 종목 열 전체 재수집 = miadjprice(`daily_uss_stock_split.py`), 2025-02-06 일괄 적재 이력 = 원천 코드 미확인(전 기간 상수비 차이는 miadjprice 원장과 정합하는 정황). "
              "stale_split_basis = v1/v2.1 비율이 CIQ 분할계수(비)와 일치·재수집 안 됨(옛 기준 셀); mi_repull_split_ledger_diff = 재수집 종목인데 CIQ 분할비만큼 차이(MI≠CIQ 분할 원장); "
              "constant_ratio_run = 종목 차이 셀 60%↑가 한 비율 공유(≥20셀, 분할비 아님: 권리락·주식배당·MI≠CIQ 조정 원장); "
              "qflag2 = v2.1 qualityFlag 2(ciq·mi raw 종가 불일치); small_revision/other_large = 그 외(잠정가·정정·반올림).\n")
    md.append(md_table(t_cause))
    # 날짜별 차이 셀 상위(v1 잠정가 수집일 후보)
    by_date = cells.groupby("date").agg(셀=("tradingItemId", "size"), 중앙_상대차=("rel_diff", "median"),
                                        small_revision_비율=("cause", lambda c: float(c.str.startswith("small_revision").mean()))).reset_index()
    by_date = by_date.sort_values("셀", ascending=False)
    save_table(by_date, out, f"close_diff_by_date_{tag}")
    md.append(f"\n날짜별 차이 셀 상위 10(한 날짜에 수백 종목이 소폭 어긋나면 v1 이 그날 잠정가를 받고 재수집하지 않은 것; 전체 `close_diff_by_date_{tag}.csv`):\n")
    md.append(md_table(by_date, max_rows=10))
    summ["close_diff_top_dates"] = {str(pd.Timestamp(d).date()): int(n) for d, n in zip(by_date["date"].head(5), by_date["셀"].head(5))}
    summ["close_cause_counts"] = t_cause.set_index("cause")["셀"].to_dict() if len(t_cause) else {}
    # 종목별 상위
    if len(cells):
        g = cells.groupby("tradingItemId")
        t_id = g.agg(diff_cells=("date", "size"), first=("date", "min"), last=("date", "max"), median_ratio=("ratio_v1_over_v21", "median"),
                     max_rel=("rel_diff", "max"), cause=("cause", lambda s: s.value_counts().index[0])).reset_index()
        t_id["symbol"] = [v2.symbol(i) for i in t_id["tradingItemId"]]
        t_id["companyName"] = [v2.name(i) for i in t_id["tradingItemId"]]
        cols_idx = pd.Index(al.ids).get_indexer(t_id["tradingItemId"].to_numpy())
        for u in ("Top500", "ALL"):
            if u in al.v1_univ:
                t_id[f"v1 {u} 멤버-일(차이 셀)"] = [int((al.v1_univ[u][:, k] & sig[:, k]).sum()) for k in cols_idx]
        t_id = t_id.sort_values(["diff_cells"], ascending=False)
        save_table(t_id, out, f"close_diff_by_id_{tag}")
        md.append(f"\n종목별 상위 {top}(차이 셀 수 순; 전체 `close_diff_by_id_{tag}.csv`):\n")
        md.append(md_table(t_id, max_rows=top))
        # 유니버스 멤버 셀 기준 상위(멤버-일 차이 셀 순)
        if "v1 Top500 멤버-일(차이 셀)" in t_id:
            t_u = t_id[t_id["v1 Top500 멤버-일(차이 셀)"] > 0].sort_values("v1 Top500 멤버-일(차이 셀)", ascending=False)
            md.append(f"\nv1 Top500 멤버 기간에 걸린 차이 셀 상위 {min(top, len(t_u))}(종목 {len(t_u)}):\n")
            md.append(md_table(t_u, max_rows=top))
            summ["close_diff_ids_top500"] = int(len(t_u))
    # 꼬리 상위 종목
    if v1_tail.any():
        k = np.flatnonzero(v1_tail.any(axis=0))
        t_tail = pd.DataFrame({"tradingItemId": al.ids[k], "symbol": [v2.symbol(i) for i in al.ids[k]], "companyName": [v2.name(i) for i in al.ids[k]],
                               "v1 꼬리 셀": v1_tail[:, k].sum(axis=0),
                               "꼬리 시작": [al.dates[np.argmax(v1_tail[:, j])] for j in k],
                               "lastTradeDate": v2.lifecycle.reindex(al.ids[k])["lastTradeDate"].to_numpy(),
                               "endReason": v2.lifecycle.reindex(al.ids[k])["endReason"].to_numpy(),
                               "v1 Top500 멤버-일(꼬리)": [int((al.v1_univ["Top500"][:, j] & v1_tail[:, j]).sum()) if "Top500" in al.v1_univ else 0 for j in k]})
        t_tail = t_tail.sort_values("v1 꼬리 셀", ascending=False)
        save_table(t_tail, out, f"close_v1_tail_by_id_{tag}")
        md.append(f"\nv1 꼬리(v2.1 lastTradeDate 이후 v1 종가) 상위 {top}(전체 `close_v1_tail_by_id_{tag}.csv`):\n")
        md.append(md_table(t_tail, max_rows=top))
    return "\n".join(md), summ


def section_close(al: Aligned, v2: V21Data, out: Path, top: int) -> tuple[str, dict]:
    md = ["## 2. 종가(분할조정) — v1 라이브 vs v2.1 close split_adj\n"]
    body, summ = close_compare(al, al.v1_close, v2, out, "live", top)
    md.append(body)
    return "\n".join(md), summ


def section_close_snapshot(al: Aligned, v2: V21Data, snap_root: Path, since: pd.Timestamp, asof: pd.Timestamp | None, out: Path, top: int) -> tuple[str, dict]:
    md = [f"## 2b. 종가 — v1 고정 스냅샷({snap_root}) vs v2.1, 그리고 스냅샷 vs 라이브 v1\n"]
    d, ids, close = read_v1_wide(Path(snap_root) / "BaseData" / "close.parquet", since, asof)
    close = close.astype(np.float64, copy=False)
    r = d.get_indexer(al.dates)
    c = pd.Index(ids).get_indexer(al.ids)
    ok_r, ok_c = r >= 0, c >= 0
    snap = np.full(al.v2_close.shape, np.nan)
    snap[np.ix_(ok_r, ok_c)] = close[np.ix_(r[ok_r], c[ok_c])]
    del close
    md.append(f"스냅샷 공통 축: 날짜 {int(ok_r.sum()):,}/{len(al.dates):,}, 종목 {int(ok_c.sum()):,}/{len(al.ids):,}.\n")
    body, summ = close_compare(al, snap, v2, out, "snapshot", top, list_cells=False)
    md.append(body)
    # 스냅샷 vs 라이브
    both = np.isfinite(snap) & np.isfinite(al.v1_close)
    rel = np.abs(snap[both] - al.v1_close[both]) / np.abs(al.v1_close[both])
    n_ch = int((rel > 1e-6).sum())
    live_only = np.isfinite(al.v1_close) & ~np.isfinite(snap) & ok_r[:, None]
    snap_only = ~np.isfinite(al.v1_close) & np.isfinite(snap)
    md.append("\n### 2b-1. v1 스냅샷(2026-09-14) → 라이브 변화(공통 날짜·종목)\n")
    md.append(md_table(pd.DataFrame([
        {"항목": "공통 유효 셀", "값": int(both.sum())},
        {"항목": "값이 바뀐 셀(상대차 > 1e-6)", "값": n_ch},
        {"항목": "라이브에만 종가(스냅샷 NaN, 스냅샷 날짜 범위 내)", "값": int(live_only.sum())},
        {"항목": "스냅샷에만 종가(라이브 NaN)", "값": int(snap_only.sum())},
    ])))
    if n_ch:
        rr, cc = np.nonzero(both)
        sel = rel > 1e-6
        ch = pd.DataFrame({"date": al.dates.values[rr[sel]], "tradingItemId": al.ids[cc[sel]], "snapshot_close": snap[rr[sel], cc[sel]],
                           "live_close": al.v1_close[rr[sel], cc[sel]], "rel_diff": rel[sel]})
        ch["symbol"] = [v2.symbol(i) for i in ch["tradingItemId"]]
        save_table(ch, out, "v1_snapshot_vs_live_changed_cells", parquet=True)
        ch["ratio_snap_over_live"] = ch["snapshot_close"] / ch["live_close"]
        t = ch.groupby(["tradingItemId", "symbol"]).agg(cells=("date", "size"), first=("date", "min"), last=("date", "max"),
                                                        median_ratio=("ratio_snap_over_live", "median")).reset_index()
        t = t.sort_values("cells", ascending=False)
        md.append(f"\n바뀐 종목 상위 {top}(전체 `v1_snapshot_vs_live_changed_cells.parquet`; median_ratio = 스냅샷/라이브 → 분할 배수면 v1 이 그 사이 재기준화):\n")
        md.append(md_table(t, max_rows=top))
    summ_out = {f"snapshot_{k}": v for k, v in summ.items()}
    summ_out["v1_snapshot_to_live_changed_cells"] = n_ch
    return "\n".join(md), summ_out


# ----------------------------------------------------------------------------------------------------------
# 3. 수익률
# ----------------------------------------------------------------------------------------------------------
def label_distribution(v2: V21Data, i: int, d: pd.Timestamp) -> str:
    parts = []
    if v2.dividends is not None:
        s = v2.dividends[(v2.dividends["tradingItemId"] == i) & (v2.dividends["exDate"] == d)]
        for _, r in s.iterrows():
            kind = "stock" if bool(r.get("isStock", False)) else "cash"
            parts.append(f"{kind} type{int(r['dividendTypeId'])} ${r['divAmount_USD']:.4g}")
    if v2.distributions is not None:
        s = v2.distributions[(v2.distributions["tradingItemId"] == i) & (v2.distributions["exDate"] == d)]
        for _, r in s.iterrows():
            if r["kind"] == "stock":
                parts.append(f"spinoff→{r.get('newSymbol', '')} ratio {r.get('ratioNewPerOld', np.nan):.4g} valueFrac {r.get('impliedValueFrac', np.nan):.3g} [{r.get('status', '')}]")
            elif r["kind"] == "split":
                parts.append("split")
    return "; ".join(parts) if parts else "(no dividend/spinoff record)"


def section_returns(al: Aligned, v2: V21Data, universe: str, out: Path, top: int) -> tuple[str, dict, dict]:
    md = [f"## 3. 수익률 — v1 HoldingReturn_Usd vs v2.1 returns_total (분해: (a) 분할/정정, (b) 배당·스핀오프)\n"]
    summ = {}
    T, N = al.v2_ret.shape
    r_price = al.price_return_v21()
    fin1 = np.isfinite(al.v1_close)
    valid = fin1 & np.isfinite(al.v1_hr) & np.isfinite(al.v2_ret) & np.isfinite(r_price)
    valid[0] = False
    # v1 정의 self-check: hr == close_t / close_prev(v1 직전 유효) − 1
    prev_idx_v1 = al.prev_obs_idx(al.v1_close)
    prev_idx_v2 = al.prev_obs_idx(al.v2_close)
    hr_chk = al.v1_close / al.prev_obs_value(al.v1_close, prev_idx_v1) - 1.0
    chk = valid & np.isfinite(hr_chk)
    bad = chk & (np.abs(hr_chk - al.v1_hr) > 1e-9)
    md.append(f"v1 HoldingReturn 정의 확인: 유효 셀 {int(chk.sum()):,} 중 `close_t/close_prev − 1`(직전 유효 종가)과 1e-9 초과 차이 {int(bad.sum()):,} "
              f"({_pct(int(bad.sum()), int(chk.sum())):.2e}).\n")
    summ["v1_hr_definition_mismatch"] = int(bad.sum())

    d_total = al.v2_ret - al.v1_hr
    d_a = r_price - al.v1_hr
    d_b = al.v2_ret - r_price
    n_valid = int(valid.sum())
    # 보유 기준 멤버(t−1) — v2.1 tradable ∧ universe
    mem = al.v2_trad & al.v2_univ[universe]
    held = np.zeros_like(mem)
    held[1:] = mem[:-1]
    hv = valid & held
    rows = []
    for th in RET_THRESHOLDS:
        rows.append({"임계(|차|)": f"> {th:g}",
                     "전체: ret_total−hr": int((np.abs(d_total[valid]) > th).sum()), "전체: (a) 가격만−hr": int((np.abs(d_a[valid]) > th).sum()),
                     "전체: (b) 배당효과": int((np.abs(d_b[valid]) > th).sum()),
                     f"{universe}(t−1 멤버): total": int((np.abs(d_total[hv]) > th).sum()), f"{universe}: (a)": int((np.abs(d_a[hv]) > th).sum()),
                     f"{universe}: (b)": int((np.abs(d_b[hv]) > th).sum())})
    md.append(f"유효 셀(v1 close·hr, v2.1 ret_total·가격수익률 모두 유효, 첫 행 제외) 전체 {n_valid:,} · {universe} 보유 기준(t−1 v2.1 tradable∧universe) {int(hv.sum()):,}. "
              "(a) = v2.1 close split_adj 직전 관측 대비 수익률 − v1 hr(분할 기준·정정·결측 규약 차이), (b) = ret_total − (a) 가격수익률(배당·스핀오프 계수 효과).\n")
    md.append(md_table(pd.DataFrame(rows)))
    md.append(f"해석: 종가 레벨 차이(§2)는 대부분 상수 비율 run 이라 수익률에는 run 경계일에만 나타난다 — (a) > 1e-4 셀은 전체의 "
              f"{_pct(int((np.abs(d_a[valid]) > 1e-4).sum()), n_valid):.2e}, {universe} 보유 기준 {int((np.abs(d_a[hv]) > 1e-4).sum()):,}셀. "
              f"(b) 배당효과는 배당락일마다 D/P 크기로 나타난다.\n")
    summ["ret_valid_cells"] = n_valid
    summ["ret_frac_gt_1e-4_total"] = _pct(int((np.abs(d_total[valid]) > 1e-4).sum()), n_valid)
    summ["ret_frac_gt_1e-4_price_only"] = _pct(int((np.abs(d_a[valid]) > 1e-4).sum()), n_valid)
    summ["ret_frac_gt_1e-2_price_only"] = _pct(int((np.abs(d_a[valid]) > 1e-2).sum()), n_valid)

    # 연평균 배당 기여(동일가중 보유 북, 멤버 t−1)
    yrs = al.years
    recs = []
    for y in np.unique(yrs):
        m = (yrs == y)
        sel = hv & m[:, None]
        days = int((sel.any(axis=1)).sum())
        if days == 0:
            continue
        cnt = sel.sum(axis=1).astype(float)
        cnt[cnt == 0] = np.nan
        ew_b = np.nansum(np.where(sel, d_b, 0.0), axis=1) / cnt
        ew_a = np.nansum(np.where(sel, d_a, 0.0), axis=1) / cnt
        ew_t = np.nansum(np.where(sel, d_total, 0.0), axis=1) / cnt
        recs.append({"연도": int(y), "일수": days, "평균 종목": float(np.nanmean(cnt)), "배당효과 합(%)": float(np.nansum(ew_b)) * 100,
                     "분할/정정 효과 합(%)": float(np.nansum(ew_a)) * 100, "총차 합(%)": float(np.nansum(ew_t)) * 100,
                     "배당락 셀(b>1e-4)": int((sel & (d_b > 1e-4)).sum()), "셀": int(sel.sum())})
    t_div = pd.DataFrame(recs)
    save_table(t_div, out, "dividend_contribution_by_year")
    md.append(f"\n### 3-1. {universe} 동일가중 보유 북(멤버 t−1) 연도별 효과 합 (일별 멤버 평균 × 연간 합, %)\n")
    md.append(md_table(t_div))
    if len(t_div):
        full_years = t_div[t_div["일수"] >= 200]
        summ["dividend_contribution_annual_mean_pct"] = float(full_years["배당효과 합(%)"].mean()) if len(full_years) else float("nan")
        summ["split_correction_effect_annual_mean_pct"] = float(full_years["분할/정정 효과 합(%)"].mean()) if len(full_years) else float("nan")
        summ["dividend_contribution_by_year_pct"] = dict(zip(t_div["연도"].astype(int), t_div["배당효과 합(%)"].round(3)))

    # 종목-연도 분포(배당효과 합)
    if hv.any():
        rr, cc = np.nonzero(hv)
        df = pd.DataFrame({"year": yrs[rr], "tradingItemId": al.ids[cc], "d_b": d_b[rr, cc], "d_a": d_a[rr, cc]})
        iy = df.groupby(["tradingItemId", "year"]).agg(div_effect=("d_b", "sum"), split_corr_effect=("d_a", "sum"), days=("d_b", "size")).reset_index()
        iy_full = iy[iy["days"] >= 200]
        q = iy_full["div_effect"].quantile([0.1, 0.25, 0.5, 0.75, 0.9, 0.99]) * 100
        t_iy = pd.DataFrame([{"종목-연도(≥200일)": len(iy_full), "배당 0(<1e-4)": int((iy_full["div_effect"].abs() < 1e-4).sum()),
                              **{f"p{int(k * 100)}(%)": v for k, v in q.items()}, "max(%)": iy_full["div_effect"].max() * 100,
                              "평균(%)": iy_full["div_effect"].mean() * 100}])
        save_table(iy, out, "dividend_effect_by_id_year")
        md.append(f"\n### 3-2. 종목-연도 배당효과 합 분포({universe} 보유 셀, 연 200일 이상; 전체 `dividend_effect_by_id_year.csv`)\n")
        md.append(md_table(t_iy))

        # 배당·스핀오프 효과 상위 20: (i) 전체(멤버 셀, d_b 순) (ii) 특별배당(type 4 현금 기록) (iii) 스핀오프(type 20/stock 기록)
        top_all = df.assign(date=al.dates.values[rr])
        ri_ = pd.Index(al.dates)
        ci_ = pd.Index(al.ids)

        def enrich(t: pd.DataFrame) -> pd.DataFrame:
            t = t.copy()
            r_ = ri_.get_indexer(t["date"])
            c_ = ci_.get_indexer(t["tradingItemId"])
            t["symbol"] = [v2.symbol(i) for i in t["tradingItemId"]]
            t["v1_hr"] = al.v1_hr[r_, c_]
            t["v21_price_ret"] = r_price[r_, c_]
            t["v21_ret_total"] = al.v2_ret[r_, c_]
            t["record"] = [label_distribution(v2, int(i), pd.Timestamp(d)) for i, d in zip(t["tradingItemId"], t["date"])]
            return t[["date", "tradingItemId", "symbol", "v1_hr", "v21_price_ret", "v21_ret_total", "d_b", "record"]]

        top_b = enrich(top_all.sort_values("d_b", ascending=False).head(20))
        save_table(top_b, out, "dividend_effect_top20")
        md.append(f"\n### 3-3. 배당·스핀오프 효과(b) 상위 20 ({universe} 보유 셀; 전체 순)\n")
        md.append(md_table(top_b))
        special = spin = None
        if v2.dividends is not None:
            dv = v2.dividends
            sp_keys = dv[(dv["dividendTypeId"] == 4) & dv["isCash"].fillna(False)][["tradingItemId", "exDate"]].drop_duplicates()
            st_keys = dv[(dv["dividendTypeId"] == 20) | dv["isStock"].fillna(False)][["tradingItemId", "exDate"]].drop_duplicates()
            if v2.distributions is not None:
                st_keys = pd.concat([st_keys, v2.distributions.loc[v2.distributions["kind"] == "stock", ["tradingItemId", "exDate"]]]).drop_duplicates()
            special = top_all.merge(sp_keys.rename(columns={"exDate": "date"}), on=["tradingItemId", "date"]).sort_values("d_b", ascending=False)
            spin = top_all.merge(st_keys.rename(columns={"exDate": "date"}), on=["tradingItemId", "date"]).sort_values("d_b", ascending=False)
            special = special[~special.set_index(["tradingItemId", "date"]).index.isin(spin.set_index(["tradingItemId", "date"]).index)]
            t_sp = enrich(special.head(20))
            t_st = enrich(spin.head(20))
            save_table(t_sp, out, "special_dividend_top20")
            save_table(t_st, out, "spinoff_top20")
            md.append(f"\n특별배당(현금 type 4, 같은 날 주식 분배 없음) 상위 20 — 해당 셀 {len(special):,}, 효과 합 {special['d_b'].sum() * 100:.2f}%p(멤버 평균 아님, 셀 합):\n")
            md.append(md_table(t_sp))
            md.append(f"\n스핀오프·주식 분배(type 20/stock) 상위 20 — 해당 셀 {len(spin):,}:\n")
            md.append(md_table(t_st))
            summ["top20_has_COST"] = bool((t_sp["tradingItemId"] == 2603626).any() or (top_b["tradingItemId"] == 2603626).any())
            summ["top20_has_EBAY"] = bool((t_st["tradingItemId"] == 2608344).any() or (top_b["tradingItemId"] == 2608344).any())
            summ["special_dividend_member_cells"] = int(len(special))
            summ["spinoff_member_cells"] = int(len(spin))
        summ["dividend_effect_top20_max"] = float(top_b["d_b"].max()) if len(top_b) else float("nan")

    # 검증값: COST 2020-12-01, EBAY 2015-07-20 (공통 축에 있으면)
    chk_rows = []
    for i, d in ((2603626, "2020-12-01"), (2608344, "2015-07-20")):
        if i in set(al.ids.tolist()) and pd.Timestamp(d) in al.dates:
            k = int(np.flatnonzero(al.ids == i)[0])
            t = al.dates.get_loc(pd.Timestamp(d))
            chk_rows.append({"tradingItemId": i, "symbol": v2.symbol(i), "date": d, "v1_hr": al.v1_hr[t, k], "v21_price_ret": r_price[t, k],
                             "v21_ret_total": al.v2_ret[t, k], "d_b": d_b[t, k], f"{universe} 멤버(t−1)": bool(held[t, k]), "record": label_distribution(v2, i, pd.Timestamp(d))})
    if chk_rows:
        md.append("\n검증 사례:\n")
        md.append(md_table(pd.DataFrame(chk_rows)))

    # |차| > 10% 셀 전부(멤버 기준) + 원인
    big = hv & (np.abs(d_total) > 0.10)
    big_all = valid & (np.abs(d_total) > 0.10)
    rr, cc = np.nonzero(big)
    recs = []
    for r_, c_ in zip(rr, cc):
        i = int(al.ids[c_])
        dt = pd.Timestamp(al.dates[r_])
        db_, da_, dt_ = d_b[r_, c_], d_a[r_, c_], d_total[r_, c_]
        jump_ratio = (1 + al.v1_hr[r_, c_]) / (1 + r_price[r_, c_]) if (1 + r_price[r_, c_]) != 0 else np.nan
        R = v2.split_ratios(i)
        if abs(db_) >= 0.5 * abs(dt_):
            cause = "dividend_or_spinoff"
        elif R.size and np.isfinite(jump_ratio) and np.min(np.abs(jump_ratio / R - 1.0)) < 2e-3:
            cause = "v1_split_basis_jump"
        elif prev_idx_v1[r_, c_] != prev_idx_v2[r_, c_]:
            cause = "prev_date_mismatch(gap convention)"
        elif al.v2_qflag[r_, c_] == 2 or (prev_idx_v2[r_, c_] >= 0 and al.v2_qflag[prev_idx_v2[r_, c_], c_] == 2):
            cause = "ciq_mi_mismatch"
        elif i in v2.miadj_overwrite_ids:
            cause = "miadj_overwrite(2025-06+ split)"
        elif np.isfinite(al.v2_close[r_, c_]) and abs(al.v1_close[r_, c_] / al.v2_close[r_, c_] - 1) > 1e-2 or \
                (prev_idx_v1[r_, c_] >= 0 and np.isfinite(al.v2_close[prev_idx_v1[r_, c_], c_]) and abs(al.v1_close[prev_idx_v1[r_, c_], c_] / al.v2_close[prev_idx_v1[r_, c_], c_] - 1) > 1e-2):
            cause = "close_revision"
        else:
            cause = "other"
        recs.append({"date": dt, "tradingItemId": i, "symbol": v2.symbol(i), "v1_hr": al.v1_hr[r_, c_], "v21_price_ret": r_price[r_, c_],
                     "v21_ret_total": al.v2_ret[r_, c_], "diff_total": dt_, "diff_a_split_corr": da_, "diff_b_dividend": db_,
                     "v1_prev_date": al.dates[prev_idx_v1[r_, c_]] if prev_idx_v1[r_, c_] >= 0 else pd.NaT,
                     "v21_prev_date": al.dates[prev_idx_v2[r_, c_]] if prev_idx_v2[r_, c_] >= 0 else pd.NaT,
                     "cause": cause, "record": label_distribution(v2, i, dt) if cause == "dividend_or_spinoff" else ""})
    t_big = pd.DataFrame(recs)
    if len(t_big):
        t_big = t_big.sort_values("diff_total", key=np.abs, ascending=False)
    save_table(t_big, out, "ret_diff_gt10pct_members")
    md.append(f"\n### 3-4. |ret_total − hr| > 10% 셀: 전체 유효 셀 중 {int(big_all.sum()):,}, {universe} 보유 기준 {len(t_big):,} (보유 기준 전부 `ret_diff_gt10pct_members.csv`)\n")
    if len(t_big):
        md.append(md_table(t_big.groupby("cause").size().rename("셀").reset_index()))
        md.append(f"\n상위 {min(top, len(t_big))}(|차| 순):\n")
        md.append(md_table(t_big, max_rows=top))
    summ["ret_gt10pct_member_cells"] = int(len(t_big))
    summ["ret_gt10pct_all_cells"] = int(big_all.sum())
    summ["ret_gt10pct_causes"] = t_big["cause"].value_counts().to_dict() if len(t_big) else {}

    # 3-5. 가격수익률(a) 차이 > 10% 전 셀(유니버스 무관): v1 가짜 점프 인벤토리
    big_a = valid & (np.abs(d_a) > 0.10)
    rr, cc = np.nonzero(big_a)
    recs = []
    for r_, c_ in zip(rr, cc):
        i = int(al.ids[c_])
        jump_ratio = (1 + al.v1_hr[r_, c_]) / (1 + r_price[r_, c_]) if (1 + r_price[r_, c_]) != 0 else np.nan
        R = v2.split_ratios(i)
        if R.size and np.isfinite(jump_ratio) and np.min(np.abs(jump_ratio / R - 1.0)) < 2e-3:
            cause = "v1_split_basis_jump"
        elif prev_idx_v1[r_, c_] != prev_idx_v2[r_, c_]:
            cause = "prev_date_mismatch(gap convention)"
        elif al.v2_qflag[r_, c_] == 2 or (prev_idx_v2[r_, c_] >= 0 and al.v2_qflag[prev_idx_v2[r_, c_], c_] == 2):
            cause = "ciq_mi_mismatch"
        elif i in v2.miadj_overwrite_ids:
            cause = "mi_repull(ledger/price diff)"
        else:
            cause = "close_revision/other"
        recs.append({"date": pd.Timestamp(al.dates[r_]), "tradingItemId": i, "symbol": v2.symbol(i), "v1_hr": al.v1_hr[r_, c_], "v21_price_ret": r_price[r_, c_],
                     "diff_a": d_a[r_, c_], "jump_ratio_v1_over_v21": jump_ratio, "v1_close": al.v1_close[r_, c_], "v21_close": al.v2_close[r_, c_],
                     "cause": cause, **{f"v1 {u} 멤버(t)": bool(al.v1_univ[u][r_, c_]) for u in ("Top500", "Top1000", "ALL") if u in al.v1_univ}})
    t_a = pd.DataFrame(recs)
    if len(t_a):
        t_a = t_a.sort_values("diff_a", key=np.abs, ascending=False)
    save_table(t_a, out, "price_ret_diff_gt10pct_all")
    md.append(f"\n### 3-5. 가격수익률 차이 |(a)| > 10% 전 셀(유니버스 무관) {len(t_a):,} — v1 가짜 점프 인벤토리(전체 `price_ret_diff_gt10pct_all.csv`)\n")
    if len(t_a):
        agg = t_a.groupby("cause").agg(셀=("date", "size"), 종목=("tradingItemId", "nunique"),
                                       **{f"v1 {u} 멤버 셀": (f"v1 {u} 멤버(t)", "sum") for u in ("Top500", "Top1000", "ALL") if f"v1 {u} 멤버(t)" in t_a}).reset_index()
        md.append(md_table(agg))
        md.append(f"\n상위 {min(top, len(t_a))}(|a| 순):\n")
        md.append(md_table(t_a, max_rows=top))
        summ["price_ret_gt10pct_all_cells"] = int(len(t_a))
        summ["price_ret_gt10pct_top500_cells"] = int(t_a["v1 Top500 멤버(t)"].sum()) if "v1 Top500 멤버(t)" in t_a else None
    aux = {"r_price": r_price, "valid": valid, "d_total": d_total, "d_a": d_a, "d_b": d_b}
    return "\n".join(md), summ, aux


# ----------------------------------------------------------------------------------------------------------
# 4. 유니버스
# ----------------------------------------------------------------------------------------------------------
def _collapse_lines(mask: np.ndarray, groups: np.ndarray) -> np.ndarray:
    """열을 groups(라인 끝 id 인덱스) 별 OR 로 합친다."""
    G = int(groups.max()) + 1 if groups.size else 0
    out = np.zeros((mask.shape[0], G), dtype=np.int16)
    np.add.at(out.T, groups, mask.T.astype(np.int16))
    return out > 0


def overlap_stats(A: np.ndarray, B: np.ndarray) -> dict:
    """A = v2.1, B = v1 (T×N bool). 일별 |A∩B|/|A| 등."""
    inter = (A & B).sum(axis=1).astype(float)
    na = A.sum(axis=1).astype(float)
    nb = B.sum(axis=1).astype(float)
    union = (A | B).sum(axis=1).astype(float)
    with np.errstate(invalid="ignore", divide="ignore"):
        oa = inter / na
        ob = inter / nb
        jac = inter / union
    ok = (na > 0) & (nb > 0)
    return {"일수": int(ok.sum()), "v2.1 멤버 중앙값": float(np.median(na[ok])) if ok.any() else np.nan,
            "v1 멤버 중앙값": float(np.median(nb[ok])) if ok.any() else np.nan,
            "|A∩B|/|A| 중앙값": float(np.median(oa[ok])) if ok.any() else np.nan, "|A∩B|/|A| p05": float(np.quantile(oa[ok], 0.05)) if ok.any() else np.nan,
            "|A∩B|/|A| 최소": float(oa[ok].min()) if ok.any() else np.nan,
            "|A∩B|/|B| 중앙값": float(np.median(ob[ok])) if ok.any() else np.nan, "|A∩B|/|B| p05": float(np.quantile(ob[ok], 0.05)) if ok.any() else np.nan,
            "Jaccard 중앙값": float(np.median(jac[ok])) if ok.any() else np.nan,
            "v2.1에만 멤버-일": int((A & ~B).sum()), "v1에만 멤버-일": int((~A & B).sum()),
            "_daily": pd.DataFrame({"date": None, "n_v21": na, "n_v1": nb, "inter": inter, "ov_A": oa, "ov_B": ob, "jaccard": jac})}


def section_universe(v1: V1Data, v2: V21Data, al: Aligned, universe: str, v1m: pd.DataFrame | None, out: Path, top: int) -> tuple[str, dict]:
    md = ["## 4. 유니버스 — v1 TopUniverseV1 vs v2.1 membership (A = v2.1, B = v1; 합집합 id 축)\n"]
    summ = {}
    # 합집합 id 축(유니버스 멤버가 한 번이라도 있는 id)
    r1, r2 = al.r1, al.r2
    ids_u = set()
    for u in UNIVERSES:
        if u in v1.univ:
            ids_u |= set(v1.ids[v1.univ[u][r1].any(axis=0)].tolist())
        ids_u |= set(v2.ids[v2.univ[u][r2].any(axis=0)].tolist())
        if u in v2.univ_v1rule:
            ids_u |= set(v2.ids[v2.univ_v1rule[u][r2].any(axis=0)].tolist())
    ids_u = np.array(sorted(ids_u), dtype=np.int64)
    c1 = pd.Index(v1.ids).get_indexer(ids_u)
    c2 = pd.Index(v2.ids).get_indexer(ids_u)
    T = len(al.dates)

    def take(mask, rows, cols):
        out = np.zeros((T, ids_u.size), dtype=bool)
        ok = cols >= 0
        out[:, ok] = mask[rows][:, cols[ok]]
        return out

    line_ids = v2.line_of(ids_u)
    uniq_lines, grp = np.unique(line_ids, return_inverse=True)
    recs = []
    daily_sel = None
    masks_sel = {}
    for table, src in (("membership", v2.univ), ("membership_v1rule", v2.univ_v1rule)):
        for u in UNIVERSES:
            if u not in src or u not in v1.univ:
                continue
            A = take(src[u], r2, c2)
            B = take(v1.univ[u], r1, c1)
            st = overlap_stats(A, B)
            daily = st.pop("_daily")
            recs.append({"v2.1 표": table, "universe": u, "수준": "id", **st})
            AL = _collapse_lines(A, grp)
            BL = _collapse_lines(B, grp)
            stl = overlap_stats(AL, BL)
            stl.pop("_daily")
            recs.append({"v2.1 표": table, "universe": u, "수준": "line", **stl})
            if table == "membership" and u == universe:
                daily["date"] = al.dates.values
                daily_sel = daily
                masks_sel = {"A": A, "B": B, "AL": AL, "BL": BL}
                summ["universe_overlap_id_median"] = st["|A∩B|/|A| 중앙값"]
                summ["universe_overlap_line_median"] = stl["|A∩B|/|A| 중앙값"]
                summ["universe_symdiff_id_member_days"] = st["v2.1에만 멤버-일"] + st["v1에만 멤버-일"]
                summ["universe_symdiff_line_member_days"] = stl["v2.1에만 멤버-일"] + stl["v1에만 멤버-일"]
    t_ov = pd.DataFrame(recs)
    save_table(t_ov, out, "universe_overlap_summary")
    if daily_sel is not None:
        save_table(daily_sel, out, f"universe_overlap_daily_{universe}", parquet=True)
    md.append("line 수준 = lineage relist/rename(high/medium) 체인의 끝 id 로 열을 합친 회사 단위(v1 은 재상장 회사의 과거를 새 id 에 이어붙임).\n")
    md.append(md_table(t_ov))

    # 연도별 멤버 수·중첩(선택 유니버스)
    if daily_sel is not None:
        dd = daily_sel.copy()
        dd["year"] = pd.DatetimeIndex(dd["date"]).year
        t_y = dd.groupby("year").agg(일수=("date", "size"), v21_멤버_중앙값=("n_v21", "median"), v1_멤버_중앙값=("n_v1", "median"),
                                   중첩_id_중앙값=("ov_A", "median"), 중첩_id_최소=("ov_A", "min")).reset_index()
        md.append(f"\n### 4-1. {universe} 연도별 멤버 수·중첩(id 수준)\n")
        md.append(md_table(t_y))

    # 차이 종목·원인(선택 유니버스, membership)
    if masks_sel:
        A, B, AL, BL = masks_sel["A"], masks_sel["B"], masks_sel["AL"], masks_sel["BL"]
        only_a = A & ~B
        only_b = B & ~A
        # 라인 수준에서 상대가 멤버인 셀 → 재귀속
        AL_cols = AL[:, grp]
        BL_cols = BL[:, grp]
        reattr_a = only_a & BL_cols          # v2.1 만 멤버인데 v1 은 같은 라인의 다른 id 로 멤버
        reattr_b = only_b & AL_cols
        lc = v2.lifecycle.reindex(ids_u)
        ltd = pd.to_datetime(lc["lastTradeDate"]).values
        after_ltd = al.dates.values[:, None] > np.where(pd.isna(ltd), np.datetime64("2900-01-01"), ltd)[None, :]
        in_v1_cols = c1 >= 0
        in_v2_cols = c2 >= 0
        tokens = np.isin(ids_u, np.array(sorted(v1.tokens_to_remove), dtype=np.int64))
        pred_tokens = np.isin(line_ids, np.array(sorted(v1.tokens_to_remove), dtype=np.int64)) & ~tokens
        ins = v2.instruments.reindex(ids_u)
        in_v1_rule = ins["inV1Rule"].fillna(False).to_numpy(bool)
        cur = v2.current.reindex(ids_u)
        curprim = cur["currentPrimary"].fillna(False).to_numpy(bool)
        is_pred = np.array([int(i) in v2.line_end for i in ids_u])
        ftd = pd.to_datetime(lc["firstTradeDate"]).values
        before_ftd = al.dates.values[:, None] < np.where(pd.isna(ftd), np.datetime64("1900-01-01"), ftd)[None, :]
        recs = []
        for k in np.flatnonzero(only_a.any(axis=0) | only_b.any(axis=0)):
            i = int(ids_u[k])
            oa, ob = int(only_a[:, k].sum()), int(only_b[:, k].sum())
            ra, rb = int(reattr_a[:, k].sum()), int(reattr_b[:, k].sum())
            tail_b = int((only_b[:, k] & after_ltd[:, k]).sum())
            pre_b = int((only_b[:, k] & before_ftd[:, k] & ~reattr_b[:, k]).sum())
            causes = {}
            if ra + rb:
                causes["reattribution_old_new_id(line match)"] = ra + rb
            if pre_b:
                causes["v1_stitched_pre_firstTradeDate(line predecessor not member)"] = pre_b
            if in_v2_cols[k] and not curprim[k] and not is_pred[k] and (ob - rb - pre_b) > 0:
                causes["not_currentPrimary_in_v21"] = ob - rb - pre_b
            if not in_v1_cols[k]:
                causes["not_in_v1_cache" + ("(outside v1 rule: OTC/other exch)" if not in_v1_rule[k] else "(v1 rule ok)")] = oa - ra
            if not in_v2_cols[k]:
                causes["not_in_v21"] = ob - rb
            if tokens[k] or pred_tokens[k]:
                causes["tokens_to_remove(v1 영구 제외)"] = oa
            if tail_b:
                causes["v1_tail_after_lastTradeDate"] = tail_b
            if in_v2_cols[k] and is_pred[k] and not curprim[k] and (oa - ra) > 0:
                causes["pit_predecessor_added"] = oa - ra
            rest = oa + ob - sum(causes.values())
            if rest > 0:
                causes["rank_boundary/data_diff"] = rest
            dom = max(causes, key=causes.get) if causes else "unknown"
            recs.append({"tradingItemId": i, "symbol": v2.symbol(i) or (v1m.at[i, "symbol"] if v1m is not None and i in v1m.index else ""),
                         "companyName": v2.name(i) or (str(v1m.at[i, "companyName"])[:40] if v1m is not None and i in v1m.index else ""),
                         "lineEndId": int(line_ids[k]), "diff_days": oa + ob, "only_v21_days": oa, "only_v1_days": ob, "reattr_days": ra + rb,
                         "inV1Cache": bool(in_v1_cols[k]), "inV21": bool(in_v2_cols[k]), "inV1Rule": bool(in_v1_rule[k]), "currentPrimary": bool(curprim[k]),
                         "lastTradeDate": ltd[k], "first_diff": al.dates[np.argmax(only_a[:, k] | only_b[:, k])],
                         "last_diff": al.dates[T - 1 - np.argmax((only_a[:, k] | only_b[:, k])[::-1])], "dominant_cause": dom,
                         "causes": json.dumps(causes, ensure_ascii=False)})
        t_items = pd.DataFrame(recs).sort_values("diff_days", ascending=False) if recs else pd.DataFrame()
        save_table(t_items, out, f"universe_diff_items_{universe}")
        if len(t_items):
            t_cause = t_items.groupby("dominant_cause").agg(종목=("tradingItemId", "size"), diff_days=("diff_days", "sum")).reset_index().sort_values("diff_days", ascending=False)
            md.append(f"\n### 4-2. {universe} 차이 종목 원인(dominant, id 수준; 전체 `universe_diff_items_{universe}.csv`). "
                      f"reattribution = 같은 라인의 다른 id 로 상대가 멤버(line 수준 일치); v1_stitched_pre_firstTradeDate = v1 은 이어붙인 과거로 멤버인데 v2.1 의 라인 선행 id 는 멤버 아님(선행 id 시총·규칙 미충족, 예 DD←2606617); "
                      f"not_currentPrimary_in_v21 = v2.1 current.currentPrimary False(primaryFlagTi 0, 예 RACE) 라 후보 제외; not_in_v1_cache = v1 열 없음(OTC 이동·규칙 밖); "
                      f"pit_predecessor_added = PIT primary 로 들어온 선행 id; tokens_to_remove = v1 METADATA 46종(+선행 id); v1_tail = lastTradeDate 뒤 v1 멤버; 나머지 = 순위 경계·데이터(시총·거래대금) 차이.\n")
            md.append(md_table(t_cause))
            summ["universe_diff_items"] = int(len(t_items))
            summ["universe_cause_days"] = t_cause.set_index("dominant_cause")["diff_days"].to_dict()
            md.append(f"\n차이 종목 상위 {top}(diff_days 순):\n")
            md.append(md_table(t_items.drop(columns=["causes"]), max_rows=top))
        # v1 멤버인데 v1 close NaN 인 셀(sanity)
        v1c = np.full((T, ids_u.size), np.nan)
        v1c[:, in_v1_cols] = v1.close[np.ix_(r1, c1[in_v1_cols])]
        n_b_nan = int((B & ~np.isfinite(v1c)).sum())
        md.append(f"\nsanity: v1 {universe} 멤버-일 중 v1 close NaN 셀 {n_b_nan:,} / {int(B.sum()):,}.\n")
        summ["v1_member_close_nan_cells"] = n_b_nan
    return "\n".join(md), summ


# ----------------------------------------------------------------------------------------------------------
# 5. 포트폴리오 프록시
# ----------------------------------------------------------------------------------------------------------
def _ew_series(ret: np.ndarray, mem_prev: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    sel = mem_prev & np.isfinite(ret)
    cnt = sel.sum(axis=1).astype(float)
    with np.errstate(invalid="ignore", divide="ignore"):
        r = np.nansum(np.where(sel, ret, 0.0), axis=1) / cnt
    r[cnt == 0] = np.nan
    return r, cnt


def section_portfolio(al: Aligned, aux: dict, universe: str, out: Path) -> tuple[str, dict]:
    md = [f"## 5. 포트폴리오 영향 프록시 — {universe} 동일가중 롱 북(일별 리밸런스, t−1 멤버 보유)\n"]
    summ = {}
    T = len(al.dates)
    mem21 = al.v2_trad & al.v2_univ[universe]
    mem1 = al.v1_univ[universe] & np.isfinite(al.v1_close) if universe in al.v1_univ else mem21
    held21 = np.zeros_like(mem21); held21[1:] = mem21[:-1]
    held1 = np.zeros_like(mem1); held1[1:] = mem1[:-1]
    common_held = held21 & held1
    r_price = aux["r_price"]
    variants = {
        "A. v1 그대로(v1 Top500 멤버, v1 hr)": (al.v1_hr, held1),
        "B. 공통 멤버, v1 hr": (al.v1_hr, common_held),
        "C. 공통 멤버, v2.1 가격수익률": (r_price, common_held),
        "D. 공통 멤버, v2.1 returns_total": (al.v2_ret, common_held),
        "E. v2.1 그대로(v2.1 tradable∧Top500, returns_total)": (al.v2_ret, held21),
        "F. v2.1 멤버, v1 hr(v1 hr 없는 셀 제외)": (al.v1_hr, held21),
    }
    series = {}
    counts = {}
    for k, (ret, held) in variants.items():
        s, c = _ew_series(ret, held)
        s[0] = np.nan
        series[k] = s
        counts[k] = c
    yrs = al.years
    recs = []
    for y in np.unique(yrs):
        m = yrs == y
        m[0] = False
        rec = {"연도": int(y), "일수": int(m.sum())}
        for k in variants:
            s = series[k][m]
            rec[k.split(".")[0] + " 연수익(%)"] = float(np.nansum(s)) * 100
        rec["평균 종목 A"] = float(np.nanmean(counts["A. v1 그대로(v1 Top500 멤버, v1 hr)"][m]))
        rec["평균 종목 공통"] = float(np.nanmean(counts["B. 공통 멤버, v1 hr"][m]))
        rec["평균 종목 E"] = float(np.nanmean(counts["E. v2.1 그대로(v2.1 tradable∧Top500, returns_total)"][m]))
        rec["C−B 분할/정정(%)"] = rec["C 연수익(%)"] - rec["B 연수익(%)"]
        rec["D−C 배당(%)"] = rec["D 연수익(%)"] - rec["C 연수익(%)"]
        rec["B−A 유니버스(v1측)(%)"] = rec["B 연수익(%)"] - rec["A 연수익(%)"]
        rec["E−D 유니버스(v2.1측)(%)"] = rec["E 연수익(%)"] - rec["D 연수익(%)"]
        rec["E−A 전체 전환(%)"] = rec["E 연수익(%)"] - rec["A 연수익(%)"]
        recs.append(rec)
    t = pd.DataFrame(recs)
    save_table(t, out, "portfolio_proxy_by_year")
    md.append("변형: A = v1 마스크·v1 hr, B = 공통 마스크(v1∧v2.1 t−1 멤버)·v1 hr, C = 공통·v2.1 가격수익률, D = 공통·v2.1 returns_total, "
              "E = v2.1 마스크·returns_total, F = v2.1 마스크·v1 hr. 연수익 = 일별 동일가중 수익률의 연간 단순합(%). 셀 수익률 결측은 그 날 평균에서 제외.\n")
    md.append(md_table(t[["연도", "일수", "평균 종목 A", "평균 종목 공통", "평균 종목 E"] + [c for c in t.columns if c.endswith("연수익(%)")]]))
    md.append("\n효과 분해(연도별, %p):\n")
    md.append(md_table(t[["연도", "일수", "C−B 분할/정정(%)", "D−C 배당(%)", "B−A 유니버스(v1측)(%)", "E−D 유니버스(v2.1측)(%)", "E−A 전체 전환(%)"]]))
    # 누적·연율
    def cum_ann(s):
        s = s[1:]
        s = s[np.isfinite(s)]
        n = s.size
        cum = float(np.prod(1 + s) - 1) if n else np.nan
        ann = float((1 + cum) ** (252.0 / n) - 1) if n else np.nan
        vol = float(np.std(s, ddof=1) * np.sqrt(252)) if n > 1 else np.nan
        return n, cum, ann, vol
    recs = []
    for k in variants:
        n, cum, ann, vol = cum_ann(series[k])
        recs.append({"변형": k, "일수": n, "누적(%)": cum * 100, "연율(%)": ann * 100, "연율 변동성(%)": vol * 100,
                     "평균 종목": float(np.nanmean(counts[k][1:]))})
    t2 = pd.DataFrame(recs)
    md.append("\n전 기간 누적·연율(기하):\n")
    md.append(md_table(t2))
    a = t2.set_index("변형")
    summ["port_ann_v1_asis_pct"] = float(a.loc["A. v1 그대로(v1 Top500 멤버, v1 hr)", "연율(%)"])
    summ["port_ann_v21_asis_pct"] = float(a.loc["E. v2.1 그대로(v2.1 tradable∧Top500, returns_total)", "연율(%)"])
    summ["port_ann_dividend_effect_pct"] = float(a.loc["D. 공통 멤버, v2.1 returns_total", "연율(%)"] - a.loc["C. 공통 멤버, v2.1 가격수익률", "연율(%)"])
    summ["port_ann_split_corr_effect_pct"] = float(a.loc["C. 공통 멤버, v2.1 가격수익률", "연율(%)"] - a.loc["B. 공통 멤버, v1 hr", "연율(%)"])
    daily = pd.DataFrame({"date": al.dates.values, **{k.split(".")[0]: v for k, v in series.items()}, "n_common": counts["B. 공통 멤버, v1 hr"]})
    save_table(daily, out, "portfolio_proxy_daily", parquet=True)
    # 일별 차이가 큰 날 상위 10 (C−B, D−C)
    dd = daily.assign(split_corr=daily["C"] - daily["B"], dividend=daily["D"] - daily["C"])
    md.append("\n분할/정정 효과(C−B) |일별| 상위 10 (v1 가짜 점프·정정이 북에 준 영향):\n")
    md.append(md_table(dd.sort_values("split_corr", key=np.abs, ascending=False).head(10)[["date", "n_common", "B", "C", "split_corr"]]))
    return "\n".join(md), summ


# ----------------------------------------------------------------------------------------------------------
# 6. 일별 모드
# ----------------------------------------------------------------------------------------------------------
def section_daily(al: Aligned, v2: V21Data, aux: dict | None, universe: str, asof: pd.Timestamp, n_days: int, out: Path) -> tuple[str, dict]:
    md = [f"## 0. 일별 모드 — asof {fmt(asof)} 기준 최근 {n_days} 거래일 (운영 점검용)\n"]
    summ = {}
    idx = np.flatnonzero(al.dates.values <= np.datetime64(asof, "ns"))
    idx = idx[-n_days:]
    if idx.size == 0:
        md.append("(공통 날짜 없음)\n")
        return "\n".join(md), summ
    r_price = aux["r_price"] if aux is not None else al.price_return_v21()
    recs = []
    details = []
    for t in idx:
        d = pd.Timestamp(al.dates[t])
        f1, f2 = np.isfinite(al.v1_close[t]), np.isfinite(al.v2_close[t])
        both = f1 & f2
        rel = np.abs(al.v1_close[t][both] - al.v2_close[t][both]) / np.abs(al.v2_close[t][both])
        rv = np.isfinite(al.v1_hr[t]) & np.isfinite(al.v2_ret[t]) & f1
        dr = np.abs(al.v2_ret[t][rv] - al.v1_hr[t][rv])
        da = np.abs(r_price[t][rv] - al.v1_hr[t][rv])
        A = al.v2_trad[t] & al.v2_univ[universe][t]
        B = al.v1_univ[universe][t] if universe in al.v1_univ else A
        rec = {"date": d, "v1 종가": int(f1.sum()), "v2.1 종가": int(f2.sum()), "공통": int(both.sum()), "v1만": int((f1 & ~f2).sum()),
               "v2.1만": int((f2 & ~f1).sum()), "종가차>1e-4": int((rel > 1e-4).sum()), "종가차>1e-2": int((rel > 1e-2).sum()),
               "수익률 유효": int(rv.sum()), "|ret_total−hr|>1e-4": int((dr > 1e-4).sum()), "|가격ret−hr|>1e-4": int((da > 1e-4).sum()),
               "|가격ret−hr|>1e-2": int((da > 1e-2).sum()), f"{universe} v2.1": int(A.sum()), f"{universe} v1": int(B.sum()),
               "v2.1만 멤버": int((A & ~B).sum()), "v1만 멤버": int((~A & B).sum())}
        recs.append(rec)
        # 상세: 종가차 상위 / 유니버스 차이 id
        cb = np.flatnonzero(both)
        for pos in np.argsort(-rel)[:10]:
            if rel[pos] > 1e-4:
                j = cb[pos]
                details.append({"date": d, "kind": "close", "tradingItemId": int(al.ids[j]), "symbol": v2.symbol(int(al.ids[j])),
                                "v1": al.v1_close[t, j], "v21": al.v2_close[t, j], "diff": al.v1_close[t, j] / al.v2_close[t, j] - 1})
        cv_ = np.flatnonzero(rv)
        for pos in np.argsort(-da)[:10]:
            if da[pos] > 1e-3:
                j = cv_[pos]
                details.append({"date": d, "kind": "price_return", "tradingItemId": int(al.ids[j]), "symbol": v2.symbol(int(al.ids[j])),
                                "v1": al.v1_hr[t, j], "v21": r_price[t, j], "diff": r_price[t, j] - al.v1_hr[t, j]})
        for j in np.flatnonzero(A & ~B):
            details.append({"date": d, "kind": f"{universe}_v21_only", "tradingItemId": int(al.ids[j]), "symbol": v2.symbol(int(al.ids[j])), "v1": 0, "v21": 1, "diff": 1})
        for j in np.flatnonzero(~A & B):
            details.append({"date": d, "kind": f"{universe}_v1_only", "tradingItemId": int(al.ids[j]), "symbol": v2.symbol(int(al.ids[j])), "v1": 1, "v21": 0, "diff": -1})
    t = pd.DataFrame(recs)
    save_table(t, out, "daily_summary")
    t_det = pd.DataFrame(details)
    save_table(t_det, out, "daily_details")
    md.append(md_table(t))
    md.append(f"\n상세(종가차 상위·가격수익률 차 상위·유니버스 차이 id; 전체 `daily_details.csv`): {len(t_det):,} 행\n")
    md.append(md_table(t_det, max_rows=60))
    last = recs[-1]
    summ.update({"daily_last_date": str(last["date"].date()), "daily_last_close_diff_gt_1e-4": last["종가차>1e-4"],
                 "daily_last_price_ret_diff_gt_1e-4": last["|가격ret−hr|>1e-4"], "daily_last_universe_symdiff": last["v2.1만 멤버"] + last["v1만 멤버"],
                 "daily_last_v1_only_ids": last["v1만"], "daily_last_v21_only_ids": last["v2.1만"]})
    return "\n".join(md), summ


# ----------------------------------------------------------------------------------------------------------
# 실행
# ----------------------------------------------------------------------------------------------------------
def run(args) -> Path:
    t_start = time.perf_counter()
    since = pd.Timestamp(args.since)
    v21_root = Path(args.v21_root)
    master_root = Path(args.master_root) if args.master_root else v21_root
    manifest = json.loads((v21_root / "manifest.json").read_text()) if (v21_root / "manifest.json").exists() else {}
    asof = pd.Timestamp(args.asof) if args.asof else (pd.Timestamp(manifest["asOfDate"]) if "asOfDate" in manifest else None)
    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)

    v1 = V1Data(Path(args.v1_root), since, None)
    v2 = V21Data(v21_root, master_root, since, None, threads=args.threads, ids=getattr(args, "ids_subset", None))
    if asof is None:
        asof = min(v1.dates[np.isfinite(v1.close).any(axis=1)][-1], v2.dates[-1])
    al = Aligned(v1, v2, asof)
    v1.hr = None                    # 정렬 사본만 사용 → 원본 해제(메모리)
    v2.ret = None
    v2.close = None
    v2.cv.release()
    tag = asof.strftime("%Y-%m-%d")
    out = out_root / f"compare_{tag}"
    out.mkdir(parents=True, exist_ok=True)
    v1m = read_v1_master(Path(args.v1_master) if args.v1_master else (V1_MASTER_DEFAULT if V1_MASTER_DEFAULT.exists() else None))

    head = [f"# v1 vs cache_v2.1 비교 리포트 — asof {tag}\n",
            f"생성 {time.strftime('%Y-%m-%d %H:%M:%S')} · v1 `{args.v1_root}`(라이브, 읽기 전용) · v2.1 `{v21_root}` "
            f"(build_id `{manifest.get('build_id', '?')}`, asOfDate {manifest.get('asOfDate', '?')}) · since {args.since} · universe {args.universe}.\n",
            f"공통 축: 날짜 {len(al.dates):,}({fmt(al.dates[0])}~{fmt(al.dates[-1])}) × 종목 {len(al.ids):,}(v1 열 ∩ v2.1 종목, 기간 내 종가 있는 것). "
            f"v1 열 = str(tradingItemId) → int64. v2.1 종가 = close split_adj(lifecycle cut), 수익률 = returns_total(cut). 표는 `compare_{tag}/`.\n"]
    sections = []
    summ = {"asof": tag, "since": args.since, "universe": args.universe, "v1_root": str(args.v1_root), "v21_root": str(v21_root),
            "build_id": manifest.get("build_id"), "common_dates": int(len(al.dates)), "common_ids": int(len(al.ids))}

    aux = None
    if args.mode in ("full", "both"):
        s, x = section_ids(v1, v2, al, v1m, out, args.top); sections.append(s); summ.update(x); log("section 1 done")
        s, x = section_close(al, v2, out, args.top); sections.append(s); summ.update(x); log("section 2 done")
        if args.v1_snapshot_root:
            s, x = section_close_snapshot(al, v2, Path(args.v1_snapshot_root), since, asof, out, args.top); sections.append(s); summ.update(x); log("section 2b done")
        s, x, aux = section_returns(al, v2, args.universe, out, args.top); sections.append(s); summ.update(x); log("section 3 done")
        s, x = section_universe(v1, v2, al, args.universe, v1m, out, args.top); sections.append(s); summ.update(x); log("section 4 done")
        s, x = section_portfolio(al, aux, args.universe, out); sections.append(s); summ.update(x); log("section 5 done")
    daily_md = ""
    if args.mode in ("daily", "both"):
        daily_md, x = section_daily(al, v2, aux, args.universe, asof, args.daily_days, out); summ.update(x); log("section 0 (daily) done")

    summ["elapsed_sec"] = round(time.perf_counter() - t_start, 1)
    summ["peak_rss_gb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024, 2)
    cc = summ.get("close_cause_counts", {}) or {}
    n_cc = sum(cc.values()) or 1
    level_like = sum(v for k, v in cc.items() if k.startswith(("stale_split", "mi_repull_split", "constant_ratio")))
    key = ["## 핵심 숫자\n", md_table(pd.DataFrame([
        {"항목": "공통 종목 / v1에만 / v2.1에만 (기간 내 종가 있는 id)", "값": f"{summ.get('common_ids')} / {summ.get('v1_only_ids')} / {summ.get('v21_only_ids')}"},
        {"항목": "종가 공통 셀 상대차 > 1e-4 비율", "값": summ.get("close_frac_gt_0.0001")},
        {"항목": "종가 공통 셀 상대차 > 1e-2 비율", "값": summ.get("close_frac_gt_0.01")},
        {"항목": "  차이 셀 중 레벨(상수 비율: 옛 분할기준·MI≠CIQ 원장·권리락) 비중", "값": _pct(level_like, n_cc)},
        {"항목": "가격수익률 차이 |(a)| > 1e-4 비율 (전체 유효 셀)", "값": summ.get("ret_frac_gt_1e-4_price_only")},
        {"항목": "가격수익률 차이 |(a)| > 1e-2 비율 (전체 유효 셀)", "값": summ.get("ret_frac_gt_1e-2_price_only")},
        {"항목": f"{args.universe} 동일가중 북 배당 기여 연평균(%)", "값": summ.get("dividend_contribution_annual_mean_pct")},
        {"항목": f"{args.universe} 동일가중 북 분할/정정 효과 연평균(%)", "값": summ.get("split_correction_effect_annual_mean_pct")},
        {"항목": f"{args.universe} 중첩 |A∩B|/|A| 중앙값 (id / line)", "값": f"{fmt(summ.get('universe_overlap_id_median'))} / {fmt(summ.get('universe_overlap_line_median'))}"},
        {"항목": f"{args.universe} 대칭차 멤버-일 (id / line)", "값": f"{fmt(summ.get('universe_symdiff_id_member_days'))} / {fmt(summ.get('universe_symdiff_line_member_days'))}"},
        {"항목": "포트폴리오 연율(%) v1 그대로 → v2.1 그대로", "값": f"{fmt(summ.get('port_ann_v1_asis_pct'))} → {fmt(summ.get('port_ann_v21_asis_pct'))}"},
        {"항목": "일별(마지막 날) 종가차>1e-4 / 가격수익률차>1e-4 / 유니버스 대칭차", "값": f"{summ.get('daily_last_close_diff_gt_1e-4')} / {summ.get('daily_last_price_ret_diff_gt_1e-4')} / {summ.get('daily_last_universe_symdiff')}"},
        {"항목": "실행 시간(초) / peak RSS(GB)", "값": f"{summ['elapsed_sec']} / {summ['peak_rss_gb']}"},
    ]))]
    report = "\n".join(head + key + ([daily_md] if daily_md else []) + sections)
    md_path = out_root / f"compare_{tag}.md"
    md_path.write_text(report)
    (out / "summary.json").write_text(json.dumps(summ, ensure_ascii=False, indent=1, default=str))
    log(f"report {md_path} ({len(report) / 1024:.0f} KB), tables {out}")
    return md_path


# ----------------------------------------------------------------------------------------------------------
# 반례 self-test: v2.1 에서 합성 v1 을 만들고 차이를 심어 정확히 검출되는지
# ----------------------------------------------------------------------------------------------------------
def _write_wide(arr: np.ndarray, dates: pd.DatetimeIndex, ids: np.ndarray, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(arr, index=dates, columns=[str(i) for i in ids])
    df.to_parquet(path)


def selftest(args) -> int:
    """합성 v1(v2.1 값 복사 + 심은 차이) 로 도구가 실패를 판정할 수 있음을 보인다."""
    import shutil
    v21_root = Path(args.v21_root)
    master_root = Path(args.master_root) if args.master_root else v21_root
    out = Path(args.out) / "selftest"
    if out.exists():
        shutil.rmtree(out)
    fake_v1 = out / "fake_v1"
    since = pd.Timestamp("2019-01-01")
    asof = pd.Timestamp("2021-12-31")
    # 소수 id: AAPL(4:1 2020-08-31), COST(특별배당 2020-12-01), MSFT, 상폐 종목 1(lifecycle 종료가 범위 내), 소형 몇 개
    lc = pq.read_table(master_root / "master" / "lifecycle.parquet").to_pandas().set_index("tradingItemId")
    ended = lc[(lc["lastTradeDate"] >= "2020-06-01") & (lc["lastTradeDate"] <= "2021-06-01") & (lc["status"] == "confirmed")
               & (lc["firstTradeDate"] <= "2018-01-01") & (lc["endReason"] == "merger")].sort_index()
    cur = pq.read_table(master_root / "master" / "current.parquet", columns=["tradingItemId", "symbol"]).to_pandas().set_index("tradingItemId")
    pick = [2590360, 2603626, 2630413]           # AAPL, COST, MSFT
    dead = int(ended.index[0])
    pick.append(dead)
    ids = np.array(sorted(set(pick)), dtype=np.int64)
    cv = CacheV21(v21_root, master_root=master_root, start=since, end=asof, ids=ids, threads=4)
    dates = cv.dates()
    close = cv.get_panel("close", "split_adj", as_array=True).astype(np.float64)      # cut
    close_uncut = cv.get_panel("close", "split_adj", as_array=True, cut=None).astype(np.float64)
    ret = cv.get_returns(as_array=True, cut="lifecycle")
    univ = cv.get_mask("tradable&universe:Top500", as_array=True)
    col = {int(i): k for k, i in enumerate(cv.ids())}
    # 합성 v1 = v2.1 가격만 수익률 기준(배당 없음) → hr = close pct_change(직전 유효)
    v1c = close.copy()
    # (1) AAPL: 2020-08-31 이전 행을 옛 기준(×4) 으로 → 'v1_old_split_basis' + 경계일 가짜 점프 −75%
    a = col[2590360]
    t_split = int(np.searchsorted(dates.values, np.datetime64("2020-08-31")))
    n_old = int(np.isfinite(v1c[:t_split, a]).sum())
    v1c[:t_split, a] *= 4.0
    # (2) MSFT: 한 셀 ×1.001 → small_revision 1셀
    m = col[2630413]
    t_one = 300
    v1c[t_one, m] *= 1.001
    # (3) 상폐 종목: lastTradeDate 뒤 5 거래일에 v1 종가(꼬리) 심기(마지막 값 carry)
    dcol = col[dead]
    ltd = pd.Timestamp(lc.at[dead, "lastTradeDate"])
    t_ltd = int(np.searchsorted(dates.values, np.datetime64(ltd), side="right"))
    last_val = v1c[t_ltd - 1, dcol] if np.isfinite(v1c[t_ltd - 1, dcol]) else np.nanmax(v1c[:t_ltd, dcol])
    n_tail = min(5, len(dates) - t_ltd)
    v1c[t_ltd:t_ltd + n_tail, dcol] = last_val
    # hr(v1 규약: ffill pct_change, 상폐 후 0)
    hr = pd.DataFrame(v1c).ffill().pct_change().to_numpy()
    # (4) 유니버스: v2.1 Top500 마스크 복사 후 3 셀 반전(COST 2 일 제거, 상폐 종목 1 일 추가)
    u1 = univ.copy()
    c = col[2603626]
    flip_days = np.flatnonzero(u1[:, c])[:2]
    u1[flip_days, c] = False
    u1[10, dcol] = True
    _write_wide(v1c, dates, cv.ids(), fake_v1 / "BaseData" / "close.parquet")
    _write_wide(hr, dates, cv.ids(), fake_v1 / "Returns" / "HoldingReturn_Usd.parquet")
    for u in UNIVERSES:
        _write_wide(u1 if u == "Top500" else univ, dates, cv.ids(), fake_v1 / "TopUniverseV1" / f"{u}.parquet")
    (fake_v1 / "TopUniverseV1" / "METADATA.yml").write_text("TokensToRemove:\n- '2622876'\n")
    ns = argparse.Namespace(v1_root=str(fake_v1), v21_root=str(v21_root), master_root=str(master_root), out=str(out), since=str(since.date()),
                            universe="Top500", asof=str(asof.date()), mode="both", daily_days=5, threads=4, top=10, v1_snapshot_root=None, v1_master=None,
                            ids_subset=ids)
    md_path = run(ns)
    summ = json.loads((out / f"compare_{asof.date()}" / "summary.json").read_text())
    cells = pd.read_parquet(out / f"compare_{asof.date()}" / "close_diff_cells_live.parquet")
    checks = []
    n_split = int((cells["cause"] == "stale_split_basis").sum())
    checks.append(("AAPL 옛 기준 셀 = stale_split_basis", n_split == n_old, f"{n_split} vs 심은 {n_old}"))
    n_small = int(((cells["cause"] == "small_revision(<5%)") & (cells["tradingItemId"] == 2630413)).sum())
    checks.append(("MSFT 1셀 ×1.001 = small_revision 1", n_small == 1, str(n_small)))
    checks.append(("종가 차이 셀 총수 = 심은 수", len(cells) == n_old + 1, f"{len(cells)} vs {n_old + 1}"))
    checks.append(("v1 꼬리 셀 = 5", summ.get("close_v1_tail_cells") == n_tail, str(summ.get("close_v1_tail_cells"))))
    big = pd.read_csv(out / f"compare_{asof.date()}" / "ret_diff_gt10pct_members.csv")
    jump = big[(big["tradingItemId"] == 2590360) & (big["cause"] == "v1_split_basis_jump")]
    checks.append(("AAPL 2020-08-31 v1 가짜 점프 검출(v1_split_basis_jump)", len(jump) == 1 and str(jump["date"].iloc[0])[:10] == "2020-08-31", str(len(jump))))
    checks.append(("유니버스 대칭차 멤버-일 = 3", summ.get("universe_symdiff_id_member_days") == 3, str(summ.get("universe_symdiff_id_member_days"))))
    checks.append(("COST 2020-12-01 배당효과 상위 20 에 포함", bool(summ.get("top20_has_COST")), str(summ.get("top20_has_COST"))))
    checks.append(("배당 기여 연평균 > 0(합성 v1 은 가격수익률만)", (summ.get("dividend_contribution_annual_mean_pct") or 0) > 0.3, str(summ.get("dividend_contribution_annual_mean_pct"))))
    checks.append(("v1 hr 정의 불일치 0", summ.get("v1_hr_definition_mismatch") == 0, str(summ.get("v1_hr_definition_mismatch"))))
    # 대조: 심지 않은 것은 0 이어야 함 — 스냅샷 없음, v2.1만/ v1만 id 0
    checks.append(("v1에만 id 0 / v2.1에만 id 0", summ.get("v1_only_ids") == 0 and summ.get("v21_only_ids") == 0, f"{summ.get('v1_only_ids')}/{summ.get('v21_only_ids')}"))
    ok_all = all(c[1] for c in checks)
    lines = ["# compare_v1 self-test\n", f"fixture ids {ids.tolist()} ({', '.join(cur.reindex(ids)['symbol'].fillna('?').tolist())}), {since.date()}~{asof.date()}, 상폐 fixture {dead} lastTradeDate {ltd.date()}\n",
             md_table(pd.DataFrame([{"검사": c[0], "통과": c[1], "값": c[2]} for c in checks])), f"\n결과: {'PASS' if ok_all else 'FAIL'} ({sum(c[1] for c in checks)}/{len(checks)}) · 리포트 {md_path}\n"]
    (out / "SELFTEST.md").write_text("\n".join(lines))
    print("\n".join(lines))
    return 0 if ok_all else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", nargs="?", default="run", choices=["run", "selftest"])
    p.add_argument("--v1-root", default="/home/base/cache/cache_v1_cash_1d")
    p.add_argument("--v1-snapshot-root", default=None, help="v1 고정 스냅샷(있으면 §2b: 스냅샷 vs v2.1, 스냅샷 vs 라이브)")
    p.add_argument("--v1-master", default=None, help=f"v1 secmaster parquet(기본 {V1_MASTER_DEFAULT} 있으면 사용)")
    p.add_argument("--v21-root", required=True)
    p.add_argument("--master-root", default=None)
    p.add_argument("--out", required=True)
    p.add_argument("--since", default="2010-01-01")
    p.add_argument("--universe", default="Top500", choices=list(UNIVERSES))
    p.add_argument("--asof", default=None, help="비교 마지막 날(기본 v2.1 manifest asOfDate)")
    p.add_argument("--mode", default="both", choices=["full", "daily", "both"])
    p.add_argument("--daily-days", type=int, default=5)
    p.add_argument("--threads", type=int, default=8)
    p.add_argument("--top", type=int, default=30)
    args = p.parse_args(argv)
    pa.set_cpu_count(min(args.threads, 8))
    pa.set_io_thread_count(min(args.threads, 8))
    if args.cmd == "selftest":
        return selftest(args)
    run(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
