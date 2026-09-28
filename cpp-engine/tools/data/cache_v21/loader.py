#!/home/jgo/usr/venv-3.11/bin/python
"""cache_v2.1 파이썬 참조 로더 (T4).

long 저장(base_data.parquet) + 구간 계수(factors/{split,dividend}.parquet) + 총수익률(derived/returns_total.parquet)
+ 이력형 마스터(master/*.parquet) + universe/membership.parquet 을 읽어 **고정 tradingItemId 순서의 wide 패널**을 만든다.

역할
  1. C++ 로더의 parity 기준(산식·연산 순서·dtype 을 여기 그대로 고정)
  2. v1 비교·연구용 wide 생성기(export_wide → BaseData/<field>.parquet 등)
  3. 산출물 계약(BRIEF §3)의 실행 가능한 정의(validate_factors, FactorGapError 등)

산식 (SPEC_loader.md §4)
  price(split_adj) = float32( float64(raw) / splitFactor )
  price(div_adj)   = float32( (float64(raw) * divAdjFactor) / splitFactor )
  volume(split_adj)= float64(raw_volume) * splitFactor          (div_adj 정의 없음 → ValueError)
  구간 계수 lookup: (id, date) 에 대해 validFrom <= date < validTo 인 구간 정확히 1개 (validTo NaT = 열림)

사용
  cv = CacheV21('/path/to/stage', master_root='/path/to/T1')   # master 가 다른 디렉토리에 있으면 지정
  close = cv.get_panel('close', 'split_adj')                        # DataFrame(index=date, columns=int64 id), lastTradeDate 이후 NaN(cut='lifecycle')
  close_full = cv.get_panel('close', 'split_adj', cut=None)         # 미절단(승계사 가격 꼬리 포함, 연구용)
  ret   = cv.get_returns()                                          # derived/returns_total → wide float64
  mask  = cv.get_mask('tradable&universe:Top500')                  # 'priced', 'universe_v1rule:Top500' 도 가능
  cap   = cv.get_panel('marketCap_USD')                             # derived/marketcap.parquet(long) → wide, raw 만
  sec   = cv.get_group_panel('sector', 'broadcast_current')         # 엔진 neutralize 용 코드 패널('pit' 옵션)
  cv.export_wide('/tmp/out', groups=True)                           # + groups/{sector,industry,sectorIx,industryIx}.parquet
  cv.export_wide('/tmp/out', fields=('close','volume'), basis='split_adj', masks=('tradable',))
  cv.export_ca_ledger('/tmp/out/engine/ca_ledger_cache.parquet')   # events/ca_ledger.parquet(rc2 §3-1) → 엔진 단위(§3-3); export_wide 가 자동 호출

CLI
  loader.py info   --root R [--master-root M]
  loader.py export --root R --out DIR [--fields close,volume] [--basis split_adj] [--masks tradable] [--universes Top500]
  loader.py bench  --root R [--master-root M] [--out DIR]     # 로딩 시간·peak RSS 측정 (실데이터용)
  loader.py compare --a A.parquet --b B.parquet [--rtol 0]     # parity: 두 wide 파일 셀 단위 비교(기본 비트 동일)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import resource
import sys
import time
import warnings
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

__all__ = [
    "CacheV21", "FactorGapError", "FactorOverlapError", "ContractError",
    "PRICE_FIELDS", "VOLUME_FIELDS", "FIELDS", "BASES",
]

NS_PER_DAY = 86_400_000_000_000
DAY_OFFSET = 1 << 20          # 일 단위 키 오프셋(1900-01-01 = -25567 일 → 양수화)
OPEN_DAY = np.iinfo(np.int64).max // 4   # validTo NaT(열린 구간) 의 일 단위 표현
INT64_MIN = np.iinfo(np.int64).min

PRICE_FIELDS = ("open", "high", "low", "close", "vwap")
VOLUME_FIELDS = ("volume",)
FLAG_FIELDS = ("source", "qualityFlag")
FIELDS = PRICE_FIELDS + VOLUME_FIELDS
# derived/marketcap.parquet(long: date, tradingItemId, marketCap_USD, sharesOutstanding) 의 필드. basis 는 raw 만.
EXTRA_FIELDS = {"marketCap_USD": "marketcap", "sharesOutstanding": "marketcap"}
BASES = ("raw", "split_adj", "div_adj")
FACTOR_ALIASES = {"split": "split", "splitFactor": "split", "dividend": "dividend", "divAdjFactor": "dividend"}
V1_FILE_NAMES = {"vwap": "VWAP"}   # v1 BaseData 파일명 (공개 필드는 소문자, 파일은 VWAP.parquet)
UNIVERSE_NAMES = ("ALL", "Top1000", "Top700", "Top500", "Top300", "Top200", "Top100", "Top10")
# 그룹 패널(build_group_panels.py 규약): sector 코드 1..11 은 v1 sectorIx 순서 고정, industry 는 라벨 정렬 순 1..K,
# 미분류 NaN, wide float32, 라벨 파일 sectorIx/industryIx(0 행 None), 쓰기 옵션 동일(snappy·dictionary·통계 없음·v2.6).
SECTOR_ORDER_V1 = ("Energy", "Information Technology", "Industrials", "Materials", "Financials", "Consumer Discretionary",
                   "Consumer Staples", "Real Estate", "Communication Services", "Health Care", "Utilities")
GROUP_KINDS = {"sector": ("sector", "sectorIx"), "industry": ("simpleIndustryDescription", "industryIx")}
GROUP_MODES = ("broadcast_current", "pit")
GROUP_WRITE_OPTIONS = dict(compression="snappy", use_dictionary=True, write_statistics=False, version="2.6")
# 기업행동 원장(events/ca_ledger.parquet, rc2 §3-1) → 엔진용 export(engine/ca_ledger_cache.parquet, rc2 §3-3).
# rename/relist/merger_* 는 기존 events 파일 담당이라 제외(엔진이 거부). 단위 환산은 export_ca_ledger 참조.
CA_LEDGER_REL = Path("events") / "ca_ledger.parquet"
CA_EXPORT_REL = "engine/ca_ledger_cache.parquet"
CA_EXPORT_EXCLUDED_TYPES = ("merger_cash", "merger_stock", "rename", "relist")
CA_ESTIMATE_SOURCES = ("wi_close", "event_value", "vendor_implied")
CA_EXPORT_COLUMNS = ["tradingItemId", "exDate", "eventType", "legType", "cashPerShareCache", "childTradingItemId", "childRatioCache",
                     "childCloseAtExCache", "parentCloseAtExCache", "parentCloseBeforeCache", "parentFactorCache", "payDate",
                     "isEstimate", "status", "childValueSource", "eventId", "childRatioRaw",
                     "expiryDate", "subscriptionPriceCache", "exerciseRatioCache", "markerReason", "childValuePerShareCache", "estimateReason"]
CA_EXPORT_STALE_BARS = 5      # 부모 마지막 관측 종가가 exDate 보다 이만큼(패널 bar) 넘게 오래되면 앵커가 stale → 행 제외(카운트)
# 표시용 행(rc2f Y1): 엔진이 book 할 수 없는 leg 를 제거하지 않고 status unresolved·값 NaN 으로 내보내는 사유. 엔진은 보유 중 부모에서
# 이 행을 `unconfirmed`(unresolved_leg) 로 보고한다(docs/corporate_actions_engine_2026-09-17.md §9).
CA_MARKER_REASONS = ("no_cash_amount", "no_child", "coacthist_stale_termination")
CA_STALE_TERMINATION_TAG = "candidate:ended_after_co_acthist_latest"      # ledger_build.ENDED_MARKER_TAG (source lifecycle, D1 daily ledger)
# 추정 사유(rc2f Y1, 사용자 검토 09-21): 원장의 isEstimate/status estimated 행에 무엇이 추정인지 — ratio(교부비율·분할계수), date(배당락일 추정),
# value(자회사 가치·현금 금액 추정) — 를 '+' 로 결합해 export `estimateReason` 에 싣는다. 엔진은 시세로 평가한 leg 라도 이 표시가 있으면
# 손익 미확정(estimate_value, detail child_close:<reason> / cash:<reason>)으로 기록한다. 표시 없는 행은 빈 값.
CA_ESTIMATE_REASON_ORDER = ("ratio", "date", "value")
# events(정산) 파일의 엔진용 export(rc2f Y1-A): 발행본 events/corporate_actions.parquet 의 cache 단위 컬럼은 raw 기준(k≡1)이라
# split_adj 엔진 패널에서는 가드가 어긋난다 → SPEC_loader §7 환산 + 앵커를 split_adj 패널에서 재계산해 engine/events_cache.parquet 로.
EVENTS_REL = Path("events") / "corporate_actions.parquet"
EVENTS_EXPORT_REL = "engine/events_cache.parquet"
EVENTS_CACHE_COLUMNS = ("cashPerShareCache", "successorRatioCache", "targetCacheMark", "targetCacheMarkDate", "successorCacheClose",
                        "successorCacheCloseDate", "cacheUnitFactorTarget", "cacheUnitFactorTargetDate", "cacheUnitFactorSuccessor", "cacheUnitStatus")


class ContractError(ValueError):
    """산출물 계약(BRIEF §3) 위반."""


class FactorGapError(ContractError):
    """(id, date) 에 맞는 계수 구간이 없음(빈틈·첫 구간 이전·id 없음)."""


class FactorOverlapError(ContractError):
    """같은 id 의 계수 구간이 겹침 또는 validFrom >= validTo."""


def estimate_reason(L: pd.DataFrame) -> pd.Series:
    """원장 행별 추정 사유 문자열(CA_ESTIMATE_REASON_ORDER 순으로 '+' 결합; 추정 표시 없는 행은 ''):
      ratio = 추정 표시된 stock/rights 행 중 자회사 가격은 관측(child_close_ex/wi_close)이라 비율이 추정인 것, 또는 notes 의
              'share-count derived' / 'R3_ratio_estimated' / 'ratio_assumed' / 'assumed' / 'ratio estimated', 또는 추정 표시된 quantity 행(분할 계수)
      date  = notes/repairOf 의 ex-date inferred / ex_date_inferred / exDate_moved / move_date
      value = childValueSource ∈ {event_value, vendor_implied}(자회사 가치 추정) 또는 현금 행의 amount_* 표시(금액 추정)
    추정 표시(isEstimate 또는 status estimated)가 있는데 사유를 못 찾으면 'unspecified'."""
    notes = L.notes.fillna("").astype(str) + ";" + L.repairOf.fillna("").astype(str) if "notes" in L.columns else pd.Series("", index=L.index)
    is_est = L.isEstimate.fillna(False).astype(bool).to_numpy() | (L.status.astype(str) == "estimated").to_numpy()
    src = L.childValueSource.astype(str)
    leg = L.legType.astype(str)
    value_leg = leg.isin(["stock", "rights"]).to_numpy()
    r_ratio = ((is_est & src.isin(["child_close_ex", "wi_close"]).to_numpy() & value_leg)
               | notes.str.contains(r"share-count derived|R3_ratio_estimated|ratio_assumed|\bassumed\b|ratio estimated", regex=True).to_numpy()
               | (is_est & (leg == "quantity").to_numpy()))
    r_date = notes.str.contains(r"ex[-_ ]?date[^;|]*inferred|inferred[^;|]*ex[-_ ]?date|exDate_moved|move_date", regex=True, case=False).to_numpy()
    r_value = (src.isin(["event_value", "vendor_implied"]).to_numpy()
               | ((leg == "cash").to_numpy() & notes.str.contains(r"amount_vendor_implied|amount_from|individual_amount|amount_estimat", regex=True).to_numpy()))
    parts = {"ratio": r_ratio, "date": r_date, "value": r_value}
    out = []
    for i in range(len(L)):
        if not is_est[i]:
            out.append("")
            continue
        have = [k for k in CA_ESTIMATE_REASON_ORDER if parts[k][i]]
        out.append("+".join(have) if have else "unspecified")
    return pd.Series(out, index=L.index, dtype=object)


# ----------------------------------------------------------------------------------------------------------
# 유틸
# ----------------------------------------------------------------------------------------------------------
def _np(col: pa.ChunkedArray | pa.Array) -> np.ndarray:
    """pyarrow 컬럼 → numpy. null 은 float 의 NaN / datetime 의 NaT 로."""
    if isinstance(col, pa.ChunkedArray):
        if col.num_chunks == 0:
            return np.empty(0, dtype=col.type.to_pandas_dtype())
        col = col.combine_chunks() if col.num_chunks > 1 else col.chunk(0)
    if pa.types.is_timestamp(col.type) or pa.types.is_date(col.type):
        out = col.to_numpy(zero_copy_only=False)
        return out.astype("datetime64[ns]", copy=False)
    if pa.types.is_string(col.type) or pa.types.is_large_string(col.type):
        return np.asarray(col.to_pylist(), dtype=object)
    return col.to_numpy(zero_copy_only=False)


def _ts_i8(x) -> np.ndarray:
    """datetime64 배열 → int64 ns (NaT 는 INT64_MIN 유지)."""
    return np.asarray(x, dtype="datetime64[ns]").view("i8")


def _to_days(ns_i8: np.ndarray, what: str) -> np.ndarray:
    """int64 ns(자정) → 일 수. 자정이 아니면 계약 위반."""
    if ns_i8.size and np.any(ns_i8 % NS_PER_DAY != 0):
        bad = np.flatnonzero(ns_i8 % NS_PER_DAY != 0)[:3]
        raise ContractError(f"{what}: timestamp is not midnight at rows {bad.tolist()}")
    return ns_i8 // NS_PER_DAY


def _ts_or_none(x) -> pd.Timestamp | None:
    if x is None:
        return None
    t = pd.Timestamp(x)
    if t.tz is not None:
        t = t.tz_convert(None)
    return t.normalize()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def _peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def _stat_ts(v) -> pd.Timestamp | None:
    try:
        return pd.Timestamp(v)
    except Exception:  # noqa: BLE001 - 통계값 해석 실패 → 프루닝 안 함
        return None


# ----------------------------------------------------------------------------------------------------------
# 로더
# ----------------------------------------------------------------------------------------------------------
class CacheV21:
    """cache_v2.1 long 저장 → wide 패널 참조 로더.

    Parameters
    ----------
    root : base_data.parquet, factors/, derived/ 가 있는 디렉토리(T2 스테이지 또는 publish 루트)
    master_root : master/*.parquet 이 있는 디렉토리(기본 root)
    universe_root : universe/{membership,membership_v1rule}.parquet 이 있는 디렉토리(기본 root → root/universe/)
    ids : 고정 열 순서. None = base_data 등장 id 의 int64 오름차순, 'instruments' = master/instruments 순,
          시퀀스 = 그 순서 그대로(base_data 에 없는 id 는 전부 NaN 열, 목록 밖 id 행은 버림)
    start, end : 날짜 범위(포함). None = 전체
    threads : pyarrow 스레드 수(공유 머신 규칙 ≤ 8)
    validate : 계수 테이블 구간 검증(겹침·빈틈·양수) 을 첫 lookup 전에 수행
    missing_lifecycle : lifecycle 에 없는 id 의 tradable 처리 'open'(경고, 열림) / 'exclude'(False) / 'error'
    """

    def __init__(self, root, master_root=None, universe_root=None, ids=None, start=None, end=None,
                 threads: int = 8, validate: bool = True, missing_lifecycle: str = "open"):
        self.root = Path(root)
        self.master_root = Path(master_root) if master_root is not None else self.root
        self.universe_root = Path(universe_root) if universe_root is not None else self.root
        self.paths = {
            "base_data": self.root / "base_data.parquet",
            "split": self.root / "factors" / "split.parquet",
            "dividend": self.root / "factors" / "dividend.parquet",
            "returns_total": self.root / "derived" / "returns_total.parquet",
            "marketcap": self.root / "derived" / "marketcap.parquet",
            "instruments": self.master_root / "master" / "instruments.parquet",
            "lifecycle": self.master_root / "master" / "lifecycle.parquet",
            "lineage": self.master_root / "master" / "lineage.parquet",
            "exclusions": self.master_root / "master" / "tradable_exclusions.parquet",
            "classification_history": self.master_root / "master" / "classification_history.parquet",
            "membership": self.universe_root / "universe" / "membership.parquet",
            "membership_v1rule": self.universe_root / "universe" / "membership_v1rule.parquet",
        }
        if not self.paths["base_data"].exists():
            raise FileNotFoundError(self.paths["base_data"])
        threads = max(1, min(int(threads), 8))
        pa.set_cpu_count(threads)
        pa.set_io_thread_count(threads)
        self.threads = threads
        self._validate = validate
        if missing_lifecycle not in ("open", "exclude", "error"):
            raise ValueError("missing_lifecycle must be open/exclude/error")
        self.missing_lifecycle = missing_lifecycle
        self.start = _ts_or_none(start)
        self.end = _ts_or_none(end)
        self._ids_request = ids
        self.diagnostics: dict = {}
        self.timings: dict[str, float] = {}

        self._pf = pq.ParquetFile(self.paths["base_data"])
        self._schema = self._pf.schema_arrow
        self._check_base_schema()

        # 축·좌표(lazy)
        self._dates_i8: np.ndarray | None = None      # 달력(int64 ns), 오름차순
        self._ids: np.ndarray | None = None           # 고정 열 순서(int64)
        self._ri: np.ndarray | None = None            # 유지 행의 행 인덱스(int32)
        self._ci: np.ndarray | None = None            # 유지 행의 열 인덱스(int32)
        self._keep: np.ndarray | None = None          # 읽은 행 중 유지 마스크(bool) 또는 None(전부)
        self._groups: list[int] | None = None         # 읽는 row group 목록(None = 전체)
        self._factor_tables: dict[str, dict] = {}     # 'split'/'dividend' → 정렬된 구간 배열
        self._factor_rows: dict[str, np.ndarray] = {} # 유지 행별 계수(float64)
        self._panels: dict[tuple, np.ndarray] = {}    # (field, basis) → 2D 배열 캐시
        self._returns: np.ndarray | None = None
        self._lifecycle: pd.DataFrame | None = None
        self._membership: dict[str, pd.DataFrame] = {}
        self._lineage: pd.DataFrame | None = None
        self._exclusions: pd.DataFrame | None = None
        self._classification: pd.DataFrame | None = None
        self._group_labels: dict[str, list[str]] | None = None

    # ------------------------------------------------------------------------------------------------------
    # 스키마 검사
    # ------------------------------------------------------------------------------------------------------
    def _check_base_schema(self) -> None:
        names = set(self._schema.names)
        need = {"date", "tradingItemId"}
        if not need <= names:
            raise ContractError(f"base_data.parquet lacks columns {need - names}")
        t_date = self._schema.field("date").type
        if not pa.types.is_timestamp(t_date):
            raise ContractError(f"base_data.date must be timestamp, got {t_date}")
        t_id = self._schema.field("tradingItemId").type
        if not pa.types.is_integer(t_id):
            raise ContractError(f"base_data.tradingItemId must be integer, got {t_id}")
        notes = []
        if str(t_date) != "timestamp[ns]":
            notes.append(f"date type {t_date} (contract timestamp[ns])")
        if str(t_id) != "int64":
            notes.append(f"tradingItemId type {t_id} (contract int64)")
        for f in PRICE_FIELDS:
            if f in names and str(self._schema.field(f).type) != "float":
                notes.append(f"{f} type {self._schema.field(f).type} (contract float32)")
        if "volume" in names and str(self._schema.field("volume").type) != "double":
            notes.append(f"volume type {self._schema.field('volume').type} (contract float64)")
        self.available_fields = tuple(f for f in FIELDS + FLAG_FIELDS if f in names)
        if self.paths["marketcap"].exists():
            mc_names = set(pq.read_schema(self.paths["marketcap"]).names)
            self.available_fields += tuple(f for f in EXTRA_FIELDS if f in mc_names)
        self.diagnostics["schema_notes"] = notes
        if notes:
            warnings.warn("base_data schema deviates from contract: " + "; ".join(notes), stacklevel=2)

    # ------------------------------------------------------------------------------------------------------
    # 축·좌표
    # ------------------------------------------------------------------------------------------------------
    def _timed(self, key: str, t0: float) -> None:
        self.timings[key] = self.timings.get(key, 0.0) + (time.perf_counter() - t0)

    def _select_row_groups(self, id_set: np.ndarray | None) -> list[int] | None:
        """row group 통계로 필요한 그룹만 고른다(정확성은 이후 keep 마스크가 보장)."""
        md = self._pf.metadata
        n = md.num_row_groups
        if n <= 1 or (id_set is None and self.start is None and self.end is None):
            return None
        i_id = self._schema.get_field_index("tradingItemId")
        i_dt = self._schema.get_field_index("date")
        ids_sorted = np.sort(id_set) if id_set is not None else None
        groups = []
        for g in range(n):
            rg = md.row_group(g)
            keep = True
            if ids_sorted is not None:
                st = rg.column(i_id).statistics
                if st is not None and st.has_min_max:
                    lo, hi = int(st.min), int(st.max)
                    p = np.searchsorted(ids_sorted, lo, side="left")
                    keep = p < len(ids_sorted) and ids_sorted[p] <= hi
            if keep and (self.start is not None or self.end is not None):
                st = rg.column(i_dt).statistics
                if st is not None and st.has_min_max:
                    lo, hi = _stat_ts(st.min), _stat_ts(st.max)
                    if lo is not None and hi is not None:
                        if self.end is not None and lo > self.end:
                            keep = False
                        if self.start is not None and hi < self.start:
                            keep = False
            if keep:
                groups.append(g)
        return groups if len(groups) < n else None

    def _read_base(self, columns: Sequence[str]) -> pa.Table:
        if self._groups is None:
            return self._pf.read(columns=list(columns), use_threads=True)
        if not self._groups:
            return self._schema.empty_table().select(list(columns))
        return self._pf.read_row_groups(self._groups, columns=list(columns), use_threads=True)

    def _resolve_ids_request(self) -> np.ndarray | None:
        req = self._ids_request
        if req is None:
            return None
        if isinstance(req, str):
            if req != "instruments":
                raise ValueError("ids must be None, 'instruments' or a sequence of int64")
            p = self.paths["instruments"]
            if not p.exists():
                raise FileNotFoundError(p)
            arr = _np(pq.read_table(p, columns=["tradingItemId"]).column(0)).astype(np.int64)
        else:
            arr = np.asarray(list(req), dtype=np.int64)
        if arr.size != np.unique(arr).size:
            raise ValueError("ids contain duplicates")
        return arr

    def _load_coords(self) -> None:
        if self._ri is not None:
            return
        t0 = time.perf_counter()
        req_ids = self._resolve_ids_request()

        # 1) 달력: 전체 date 컬럼(날짜 범위만 적용; id 부분집합과 무관하게 거래일 달력 유지)
        d_all = _ts_i8(_np(self._pf.read(columns=["date"], use_threads=True).column(0)))
        if d_all.size and np.any(d_all == INT64_MIN):
            raise ContractError("base_data.date contains null")
        days_all = _to_days(d_all, "base_data.date")
        if self.start is not None or self.end is not None:
            lo = _to_days(np.array([self.start.value]), "start")[0] if self.start is not None else -np.inf
            hi = _to_days(np.array([self.end.value]), "end")[0] if self.end is not None else np.inf
            days_all = days_all[(days_all >= lo) & (days_all <= hi)]
        if days_all.size == 0:
            raise ContractError("no base_data rows in the requested date range")
        dmin, dmax = int(days_all.min()), int(days_all.max())
        present = np.zeros(dmax - dmin + 1, dtype=bool)
        present[days_all - dmin] = True
        cal_days = np.flatnonzero(present) + dmin
        del d_all, days_all
        self._dates_i8 = cal_days * NS_PER_DAY
        day_lut = np.full(dmax - dmin + 1, -1, dtype=np.int32)
        day_lut[cal_days - dmin] = np.arange(cal_days.size, dtype=np.int32)

        # 2) 좌표(row group 프루닝 후) → 행/열 인덱스
        self._groups = self._select_row_groups(req_ids)
        tbl = self._read_base(["date", "tradingItemId"])
        ids_rows = _np(tbl.column("tradingItemId")).astype(np.int64, copy=False)
        d_rows = _ts_i8(_np(tbl.column("date")))
        del tbl
        days_rows = _to_days(d_rows, "base_data.date")
        del d_rows
        in_range = (days_rows >= dmin) & (days_rows <= dmax)
        ri = np.full(days_rows.size, -1, dtype=np.int32)
        ri[in_range] = day_lut[days_rows[in_range] - dmin]
        keep = ri >= 0
        del days_rows, in_range

        sorted_by_id = bool(ids_rows.size < 2 or np.all(ids_rows[1:] >= ids_rows[:-1]))
        self.diagnostics["base_sorted_by_id"] = sorted_by_id
        if ids_rows.size == 0:
            uids = np.empty(0, dtype=np.int64)
            cu = np.empty(0, dtype=np.int64)
        elif sorted_by_id:
            starts = np.flatnonzero(np.r_[True, ids_rows[1:] != ids_rows[:-1]])
            uids = ids_rows[starts]
            run_len = np.diff(np.r_[starts, ids_rows.size])
            cu = np.repeat(np.arange(uids.size, dtype=np.int64), run_len)
        else:
            uids, cu = np.unique(ids_rows, return_inverse=True)
            warnings.warn("base_data is not sorted by tradingItemId (contract: sorted by (tradingItemId, date))",
                          stacklevel=2)
        del ids_rows

        if req_ids is None:
            self._ids = uids.astype(np.int64)
            ci = cu.astype(np.int32)
        else:
            self._ids = req_ids
            order = np.argsort(req_ids)
            sorted_req = req_ids[order]
            pos = np.searchsorted(sorted_req, uids)
            posc = np.minimum(pos, sorted_req.size - 1)
            found = sorted_req[posc] == uids
            remap = np.full(uids.size, -1, dtype=np.int32)
            remap[found] = order[posc[found]].astype(np.int32)
            ci = remap[cu]
            keep &= ci >= 0
            self.diagnostics["ids_requested_absent_from_base"] = int(req_ids.size - int(found.sum()))
        del cu

        if not keep.all():
            self._keep = keep
            ri = ri[keep]
            ci = ci[keep]
        else:
            self._keep = None
        self._ri, self._ci = ri, ci.astype(np.int32, copy=False)

        # 3) (id, date) 중복 검사: 선형 키 유일성
        lin = self._ci.astype(np.int64) * self._dates_i8.size + self._ri
        if lin.size > 1:
            if np.all(lin[1:] > lin[:-1]):
                dup = 0
            else:
                dup = int(lin.size - np.unique(lin).size)
            if dup:
                raise ContractError(f"base_data has {dup} duplicate (tradingItemId, date) rows")
        self.diagnostics["rows_kept"] = int(self._ri.size)
        self._timed("coords", t0)

    def _rows_days(self) -> np.ndarray:
        """유지 행의 날짜(일 수)."""
        self._load_coords()
        return (self._dates_i8[self._ri] // NS_PER_DAY)

    # 공개 축 ----------------------------------------------------------------------------------------------
    def dates(self) -> pd.DatetimeIndex:
        """거래일 달력(오름차순, 자정 timestamp[ns])."""
        self._load_coords()
        return pd.DatetimeIndex(self._dates_i8.view("datetime64[ns]"))

    def ids(self) -> np.ndarray:
        """고정 열 순서의 tradingItemId(int64)."""
        self._load_coords()
        return self._ids.copy()

    @property
    def shape(self) -> tuple[int, int]:
        self._load_coords()
        return (self._dates_i8.size, self._ids.size)

    # ------------------------------------------------------------------------------------------------------
    # 원시 컬럼 → 행 값 → 패널
    # ------------------------------------------------------------------------------------------------------
    def _read_rows(self, field: str) -> np.ndarray:
        """base_data 의 한 컬럼을 유지 행 순서로(계약 dtype 으로 캐스팅)."""
        if field not in self._schema.names:
            raise KeyError(f"field '{field}' not in base_data (available: {self.available_fields})")
        self._load_coords()
        t0 = time.perf_counter()
        v = _np(self._read_base([field]).column(0))
        if self._keep is not None:
            v = v[self._keep]
        if field in PRICE_FIELDS:
            v = v.astype(np.float32, copy=False)
        elif field in VOLUME_FIELDS:
            v = v.astype(np.float64, copy=False)
        self._timed(f"read:{field}", t0)
        return v

    def _scatter(self, values: np.ndarray, fill, dtype) -> np.ndarray:
        out = np.full((self._dates_i8.size, self._ids.size), fill, dtype=dtype)
        out[self._ri, self._ci] = values
        return out

    def _wrap(self, arr: np.ndarray, as_array: bool):
        if as_array:
            return arr
        return pd.DataFrame(arr, index=self.dates(), columns=pd.Index(self._ids, name="tradingItemId"), copy=False)

    def _factor_rows_for(self, name: str) -> np.ndarray:
        """유지 행별 계수(float64). name ∈ {'split','dividend'}."""
        if name not in self._factor_rows:
            tbl = self._factor_table(name)
            self._load_coords()
            t0 = time.perf_counter()
            self._factor_rows[name] = self._lookup(tbl, self._ci, self._rows_days(), name)
            self._timed(f"factor_rows:{name}", t0)
        return self._factor_rows[name]

    def _cut_mask(self) -> np.ndarray | None:
        """lifecycle 절단 마스크(T×N bool): date > lastTradeDate 인 셀 True. firstTradeDate 이전은 절단하지 않음(워밍업용),
        tradable_exclusions 구간도 절단하지 않음(마스크로만). lifecycle 파일이 없으면 None(경고 1회, 절단 없음).
        lifecycle 에 없는 id 는 절단하지 않고 개수만 기록."""
        if hasattr(self, "_cut_mask_cache"):
            return self._cut_mask_cache
        if not self.paths["lifecycle"].exists():
            warnings.warn("lifecycle.parquet not found: cut='lifecycle' has no effect (panels are uncut)", stacklevel=3)
            self.diagnostics["cut"] = {"mode": "lifecycle", "status": "lifecycle_missing"}
            self._cut_mask_cache = None
            return None
        lc = self.lifecycle()
        T, N = self.shape
        present = np.isin(self._ids, lc.index.to_numpy())
        last = np.full(N, np.iinfo(np.int64).max, dtype=np.int64)          # 없는 id·NaT = 절단 없음
        sub = lc.reindex(self._ids[present])
        l = _ts_i8(pd.to_datetime(sub["lastTradeDate"]).values)
        last[present] = np.where(l == INT64_MIN, np.iinfo(np.int64).max, l)
        mask = self._dates_i8[:, None] > last[None, :]
        self.diagnostics["cut"] = {"mode": "lifecycle", "ids_missing_lifecycle": int((~present).sum()),
                                   "ids_with_cut": int((last < self._dates_i8[-1]).sum()), "cells_after_lastTradeDate": int(mask.sum())}
        self._cut_mask_cache = mask
        return mask

    def _apply_cut(self, arr: np.ndarray, fill, label: str) -> np.ndarray:
        """절단본 생성(복사). 제거된 유효 셀 수를 diagnostics['cut_cells'][label] 에 기록."""
        mask = self._cut_mask()
        if mask is None:
            return arr
        out = arr.copy()
        if np.issubdtype(out.dtype, np.floating):
            removed = int(np.isfinite(out[mask]).sum())
        else:
            removed = int((out[mask] != fill).sum())
        out[mask] = fill
        self.diagnostics.setdefault("cut_cells", {})[label] = removed
        return out

    def get_panel(self, field: str, basis: str = "raw", as_array: bool = False, dtype=None, cut: str | None = "lifecycle"):
        """wide 패널. field ∈ open/high/low/close/vwap/volume(/source/qualityFlag/marketCap_USD/sharesOutstanding),
        basis ∈ raw/split_adj/div_adj, cut ∈ 'lifecycle'(기본: lifecycle.lastTradeDate 이후 셀 NaN — 엔진 입력용 절단본) | None(미절단).

        dtype 규칙(parity): 가격 float32, volume float64. 조정은 float64 로 계산 후 캐스팅.
          split_adj price  = float32(float64(raw) / sf)
          div_adj  price   = float32((float64(raw) * df) / sf)
          split_adj volume = float64(raw) * sf ;  div_adj volume → ValueError
        절단은 값 계산 뒤 마지막에 적용하며 firstTradeDate 이전·exclusions 구간은 절단하지 않는다.
        """
        if basis not in BASES:
            raise ValueError(f"basis must be one of {BASES}")
        if cut not in (None, "lifecycle"):
            raise ValueError("cut must be 'lifecycle' or None")
        if field in FLAG_FIELDS:
            if basis != "raw":
                raise ValueError(f"{field} has only basis='raw'")
            key = (field, "raw")
            if key not in self._panels:
                v = self._read_rows(field)
                self._panels[key] = self._scatter(v.astype(np.int16), -1, np.int16)
            fill = -1
        elif field in EXTRA_FIELDS:
            if basis != "raw":
                raise ValueError(f"{field} has only basis='raw' (market cap is basis-free; shares are as-reported)")
            key = (field, "raw")
            if key not in self._panels:
                arr, stats = self._pivot_long(self.paths[EXTRA_FIELDS[field]], field, strict=False, label=field)
                self.diagnostics[f"{field}_rows"] = stats
                self._panels[key] = arr
            fill = np.nan
        else:
            if field not in FIELDS:
                raise KeyError(f"unknown field '{field}' (use {FIELDS + tuple(EXTRA_FIELDS) + FLAG_FIELDS})")
            if field in VOLUME_FIELDS and basis == "div_adj":
                raise ValueError("volume has no div_adj basis (dividend factor does not rescale share counts); "
                                 "use 'split_adj' (= raw × splitFactor) or 'raw'")
            key = (field, basis)
            if key not in self._panels:
                raw = self._read_rows(field)
                t0 = time.perf_counter()
                if basis == "raw":
                    vals = raw
                else:
                    sf = self._factor_rows_for("split")
                    if field in VOLUME_FIELDS:
                        vals = raw.astype(np.float64) * sf
                    else:
                        v64 = raw.astype(np.float64)
                        if basis == "div_adj":
                            v64 = v64 * self._factor_rows_for("dividend")
                        vals = (v64 / sf).astype(np.float32)
                out_dtype = np.float64 if field in VOLUME_FIELDS else np.float32
                self._panels[key] = self._scatter(vals.astype(out_dtype, copy=False), np.nan, out_dtype)
                self._timed(f"panel:{field}:{basis}", t0)
            fill = np.nan
        if cut == "lifecycle":
            ckey = key + ("cut",)
            if ckey not in self._panels:
                self._panels[ckey] = self._apply_cut(self._panels[key], fill, f"{field}:{key[1]}")
            arr = self._panels[ckey]
        else:
            arr = self._panels[key]
        if dtype is not None and np.dtype(dtype) != arr.dtype:
            arr = arr.astype(dtype)
        return self._wrap(arr, as_array)

    def release(self, *keys) -> None:
        """패널 캐시 해제(메모리). 인자 없으면 전부."""
        if not keys:
            self._panels.clear()
            self._factor_rows.clear()
            self._returns = None
            self.__dict__.pop("_cut_mask_cache", None)
        else:
            for k in keys:
                self._panels.pop(k, None)

    # ------------------------------------------------------------------------------------------------------
    # 구간 계수
    # ------------------------------------------------------------------------------------------------------
    def _factor_table(self, name: str) -> dict:
        name = FACTOR_ALIASES.get(name, name)
        if name not in ("split", "dividend"):
            raise KeyError("factor name must be split/splitFactor/dividend/divAdjFactor")
        if name in self._factor_tables:
            return self._factor_tables[name]
        self._load_coords()
        p = self.paths[name]
        if not p.exists():
            raise FileNotFoundError(p)
        t0 = time.perf_counter()
        t = pq.read_table(p, columns=["tradingItemId", "validFrom", "validTo", "factor"])
        fid = _np(t.column("tradingItemId")).astype(np.int64, copy=False)
        vf = _ts_i8(_np(t.column("validFrom")))
        vt = _ts_i8(_np(t.column("validTo")))
        fac = np.asarray(_np(t.column("factor")), dtype=np.float64)
        if np.any(vf == INT64_MIN):
            raise ContractError(f"{name}: validFrom contains NaT")
        from_day = _to_days(vf, f"{name}.validFrom")
        open_mask = vt == INT64_MIN
        to_day = np.full(vt.size, OPEN_DAY, dtype=np.int64)
        to_day[~open_mask] = _to_days(vt[~open_mask], f"{name}.validTo")
        if np.any(~np.isfinite(fac)) or np.any(fac <= 0):
            bad = np.flatnonzero(~np.isfinite(fac) | (fac <= 0))[:5]
            raise ContractError(f"{name}: non-positive/non-finite factor at rows {bad.tolist()} ids {fid[bad].tolist()}")

        # 패널 열 인덱스로 매핑(패널 밖 id 는 버림)
        order = np.argsort(self._ids, kind="stable")
        sorted_ids = self._ids[order]
        pos = np.searchsorted(sorted_ids, fid)
        posc = np.minimum(pos, sorted_ids.size - 1)
        found = sorted_ids[posc] == fid
        idx = np.full(fid.size, -1, dtype=np.int64)
        idx[found] = order[posc[found]]
        n_dropped = int((~found).sum())
        fid, from_day, to_day, fac, idx = fid[found], from_day[found], to_day[found], fac[found], idx[found]

        srt = np.lexsort((from_day, idx))
        fid, from_day, to_day, fac, idx = fid[srt], from_day[srt], to_day[srt], fac[srt], idx[srt]
        tbl = {"idx": idx, "from_day": from_day, "to_day": to_day, "factor": fac, "id": fid,
               "key": (idx << 32) | (from_day + DAY_OFFSET), "n_dropped_ids_rows": n_dropped}
        if np.any(from_day + DAY_OFFSET < 0) or np.any(from_day + DAY_OFFSET >= (1 << 32)):
            raise ContractError(f"{name}: validFrom outside supported range")
        tbl["validation"] = self._validate_table(tbl, name, strict=self._validate)
        self._factor_tables[name] = tbl
        self._timed(f"factor_table:{name}", t0)
        return tbl

    @staticmethod
    def _validate_table(tbl: dict, name: str, strict: bool) -> dict:
        """같은 id 안에서 구간이 validFrom 순으로 빈틈·겹침 없이 이어지는지, validFrom < validTo 인지 검사."""
        idx, fd, td = tbl["idx"], tbl["from_day"], tbl["to_day"]
        res = {"rows": int(idx.size), "ids": int(np.unique(idx).size) if idx.size else 0}
        if idx.size == 0:   # 패널 id 에 해당하는 구간이 없음(부분집합 로딩) → 검사할 것 없음
            res.update({"invalid_rows": 0, "gaps": 0, "overlaps": 0, "ids_first_from_after_1900": 0, "ids_without_open_last": 0})
            return res
        invalid = fd >= td
        same = idx[1:] == idx[:-1]
        gap = same & (fd[1:] > td[:-1])
        overlap = same & (fd[1:] < td[:-1])
        first = np.r_[True, ~same]
        res.update({
            "invalid_rows": int(invalid.sum()), "gaps": int(gap.sum()), "overlaps": int(overlap.sum()),
            "ids_first_from_after_1900": int((fd[first] > _to_days(np.array([pd.Timestamp("1900-01-01").value]), "x")[0]).sum()),
            "ids_without_open_last": int((td[np.r_[~same, True]] != OPEN_DAY).sum()),
        })
        if strict:
            if res["invalid_rows"] or res["overlaps"]:
                ex = np.flatnonzero(np.r_[False, overlap] | invalid)[:5]
                raise FactorOverlapError(
                    f"{name}: {res['overlaps']} overlapping and {res['invalid_rows']} invalid intervals; "
                    f"e.g. ids {tbl['id'][ex].tolist()} validFrom(days) {fd[ex].tolist()}")
            if res["gaps"]:
                ex = np.flatnonzero(np.r_[False, gap])[:5]
                raise FactorGapError(
                    f"{name}: {res['gaps']} gaps between intervals of the same id; "
                    f"e.g. ids {tbl['id'][ex].tolist()} validFrom(days) {fd[ex].tolist()} "
                    f"(previous validTo {td[ex - 1].tolist()})")
        return res

    def validate_factors(self, strict: bool = True) -> dict:
        """두 계수 테이블의 구간 검증 결과(계약: 겹침 0·빈틈 0·첫 구간 1900-01-01·마지막 열림)."""
        out = {}
        for name in ("split", "dividend"):
            tbl = self._factor_table(name)
            out[name] = self._validate_table(tbl, name, strict=strict)
        return out

    @staticmethod
    def _lookup(tbl: dict, ci: np.ndarray, days: np.ndarray, name: str) -> np.ndarray:
        """(열 인덱스, 일 수) → 계수. 반개구간 [validFrom, validTo). 맞는 구간이 없으면 FactorGapError."""
        key = (ci.astype(np.int64) << 32) | (days.astype(np.int64) + DAY_OFFSET)
        pos = np.searchsorted(tbl["key"], key, side="right") - 1
        ok = pos >= 0
        posc = np.where(ok, pos, 0)
        ok &= (tbl["idx"][posc] == ci) & (days < tbl["to_day"][posc])
        if not ok.all():
            bad = np.flatnonzero(~ok)
            ex_days = days[bad[:5]]
            raise FactorGapError(
                f"{name}: no interval for {bad.size} (id, date) cells; e.g. column idx {ci[bad[:5]].tolist()} "
                f"dates {[str(pd.Timestamp(int(d) * NS_PER_DAY)) [:10] for d in ex_days]}")
        return tbl["factor"][posc]

    def factor_at(self, name: str, ids: Sequence[int], dates) -> np.ndarray:
        """점 lookup: 같은 길이의 ids·dates 에 대한 계수(float64). 패널 밖 id 나 빈틈이면 FactorGapError."""
        tbl = self._factor_table(name)
        ids_arr = np.asarray(ids, dtype=np.int64)
        d_i8 = _ts_i8(pd.DatetimeIndex(pd.to_datetime(dates)).values)
        if ids_arr.size != d_i8.size:
            raise ValueError("ids and dates must have the same length")
        order = np.argsort(self._ids, kind="stable")
        sorted_ids = self._ids[order]
        pos = np.searchsorted(sorted_ids, ids_arr)
        posc = np.minimum(pos, sorted_ids.size - 1)
        found = sorted_ids[posc] == ids_arr
        if not found.all():
            raise FactorGapError(f"ids not in panel: {ids_arr[~found][:5].tolist()}")
        ci = order[posc].astype(np.int32)
        return self._lookup(tbl, ci, _to_days(d_i8, "dates"), name)

    def get_factor(self, name: str, dense: bool = False, as_array: bool = False):
        """계수 wide(float64). dense=False: base_data 행이 있는 셀만(나머지 NaN). dense=True: 모든 (date, id)."""
        name = FACTOR_ALIASES.get(name, name)
        tbl = self._factor_table(name)
        if not dense:
            key = (f"factor:{name}", "rows")
            if key not in self._panels:
                self._panels[key] = self._scatter(self._factor_rows_for(name), np.nan, np.float64)
            return self._wrap(self._panels[key], as_array)
        key = (f"factor:{name}", "dense")
        if key not in self._panels:
            T, N = self.shape
            days = self._dates_i8 // NS_PER_DAY
            out = np.full((T, N), np.nan, dtype=np.float64)
            idx, fd, td, fac = tbl["idx"], tbl["from_day"], tbl["to_day"], tbl["factor"]
            bounds = np.searchsorted(idx, np.arange(N + 1))
            for j in range(N):
                a, b = bounds[j], bounds[j + 1]
                if a == b:
                    continue
                p = np.searchsorted(fd[a:b], days, side="right") - 1
                ok = p >= 0
                pc = np.where(ok, p, 0)
                ok &= days < td[a:b][pc]
                col = np.where(ok, fac[a:b][pc], np.nan)
                out[:, j] = col
            self._panels[key] = out
        return self._wrap(self._panels[key], as_array)

    # ------------------------------------------------------------------------------------------------------
    # 수익률
    # ------------------------------------------------------------------------------------------------------
    def read_returns_long(self, columns: Sequence[str] = ("date", "tradingItemId", "ret", "prevDate")) -> pd.DataFrame:
        p = self.paths["returns_total"]
        if not p.exists():
            raise FileNotFoundError(p)
        return pq.read_table(p, columns=list(columns)).to_pandas()

    def _pivot_long(self, path: Path, value_col: str, strict: bool, label: str) -> tuple[np.ndarray, dict]:
        """long(date, tradingItemId, value) → wide float64. row group 단위 스트리밍(일시 메모리 = row group 1개분).

        strict=True(returns): 달력 밖 날짜·패널 밖 id 행이 있으면 ContractError(전체 로딩일 때).
        strict=False(marketcap 등): 그런 행은 버리고 개수만 기록. (id, date) 중복은 항상 오류."""
        self._load_coords()
        if not path.exists():
            raise FileNotFoundError(path)
        pf = pq.ParquetFile(path)
        order = np.argsort(self._ids, kind="stable")
        sorted_ids = self._ids[order]
        T = self._dates_i8.size
        out = np.full(self.shape, np.nan, dtype=np.float64)
        filled = np.zeros(self.shape, dtype=bool) if pf.metadata.num_row_groups > 1 else None
        n_kept = n_out_cal = n_out_id = n_dup = 0
        for g in range(pf.metadata.num_row_groups):
            t = pf.read_row_group(g, columns=["date", "tradingItemId", value_col], use_threads=True)
            rid = _np(t.column("tradingItemId")).astype(np.int64, copy=False)
            rd = _ts_i8(_np(t.column("date")))
            val = np.asarray(_np(t.column(value_col)), dtype=np.float64)
            del t
            ri = np.searchsorted(self._dates_i8, rd)
            ric = np.minimum(ri, T - 1)
            in_cal = self._dates_i8[ric] == rd
            pos = np.searchsorted(sorted_ids, rid)
            posc = np.minimum(pos, sorted_ids.size - 1)
            found = sorted_ids[posc] == rid
            n_out_cal += int((~in_cal).sum())
            n_out_id += int((~found).sum())
            keep = in_cal & found
            ci = order[posc[keep]].astype(np.int32)
            ri = ric[keep].astype(np.int32)
            if filled is not None:
                n_dup += int(filled[ri, ci].sum())
                filled[ri, ci] = True
            else:
                lin = ci.astype(np.int64) * T + ri
                if lin.size > 1 and not np.all(lin[1:] > lin[:-1]) and lin.size != np.unique(lin).size:
                    n_dup += lin.size - np.unique(lin).size
            out[ri, ci] = val[keep]
            n_kept += int(keep.sum())
        del filled
        full_load = self.start is None and self.end is None and self._ids_request is None
        if strict and full_load and n_out_cal:
            raise ContractError(f"{label} has {n_out_cal} rows whose date is not in base_data calendar")
        if strict and self._ids_request is None and n_out_id:
            raise ContractError(f"{label} has {n_out_id} rows whose id is not in base_data")
        if n_dup:
            raise ContractError(f"{label} has {n_dup} duplicate (tradingItemId, date) rows")
        return out, {"rows_kept": n_kept, "rows_outside_calendar": n_out_cal, "rows_unknown_id": n_out_id}

    def get_returns(self, as_array: bool = False, cut: str | None = None):
        """derived/returns_total.parquet → wide float64(행 없는 셀 NaN). 달력·id 축은 base_data 와 동일.
        기본 미절단(파일 규약: lifecycle 마스킹 없음); cut='lifecycle' 이면 lastTradeDate 이후 NaN(export 기본)."""
        if self._returns is None:
            t0 = time.perf_counter()
            self._returns, stats = self._pivot_long(self.paths["returns_total"], "ret", strict=True, label="returns_total")
            self.diagnostics["returns_rows_kept"] = stats["rows_kept"]
            self._timed("returns", t0)
        if cut == "lifecycle":
            return self._wrap(self._apply_cut(self._returns, np.nan, "returns_total"), as_array)
        if cut is not None:
            raise ValueError("cut must be 'lifecycle' or None")
        return self._wrap(self._returns, as_array)

    def compute_returns(self, as_array: bool = False):
        """계약 산식으로 재계산한 총수익률: adj = float64(close_raw)·df/sf, ret = adj_t/adj_prev − 1 (직전 관측 행).

        close_raw 는 저장 float32 를 float64 로 올려 사용(T2 와 동일 입력). returns_total 과의 대조용.
        """
        raw = self._read_rows("close")
        sf = self._factor_rows_for("split")
        df = self._factor_rows_for("dividend")
        adj = self._scatter((raw.astype(np.float64) * df) / sf, np.nan, np.float64)
        prev = pd.DataFrame(adj).ffill().shift(1).to_numpy()
        out = adj / prev - 1.0
        out[np.isnan(adj)] = np.nan
        return self._wrap(out, as_array)

    def check_returns(self, atol: float = 1e-9) -> dict:
        """returns_total(파일) vs compute_returns(재계산) 대조 요약."""
        a = self.get_returns(as_array=True)
        b = self.compute_returns(as_array=True)
        both = ~np.isnan(a) & ~np.isnan(b)
        diff = np.abs(a[both] - b[both])
        return {
            "cells_file": int((~np.isnan(a)).sum()), "cells_recomputed": int((~np.isnan(b)).sum()),
            "cells_only_file": int((~np.isnan(a) & np.isnan(b)).sum()),
            "cells_only_recomputed": int((np.isnan(a) & ~np.isnan(b)).sum()),
            "max_abs_diff": float(diff.max()) if diff.size else 0.0,
            "n_diff_gt_atol": int((diff > atol).sum()), "atol": atol,
        }

    # ------------------------------------------------------------------------------------------------------
    # 마스크
    # ------------------------------------------------------------------------------------------------------
    def _read_master(self, key: str, columns: Sequence[str] | None = None) -> pd.DataFrame:
        p = self.paths[key]
        if not p.exists():
            raise FileNotFoundError(p)
        return pq.read_table(p, columns=list(columns) if columns else None).to_pandas()

    def lifecycle(self) -> pd.DataFrame:
        if self._lifecycle is None:
            lc = self._read_master("lifecycle")
            if lc["tradingItemId"].duplicated().any():
                raise ContractError("lifecycle has duplicate tradingItemId")
            self._lifecycle = lc.set_index("tradingItemId")
        return self._lifecycle

    def lineage(self) -> pd.DataFrame:
        if self._lineage is None:
            self._lineage = self._read_master("lineage")
        return self._lineage

    def membership(self, table: str = "membership") -> pd.DataFrame:
        """universe/membership.parquet (table='membership') 또는 universe/membership_v1rule.parquet ('membership_v1rule')."""
        if table not in ("membership", "membership_v1rule"):
            raise KeyError(table)
        if table not in self._membership:
            self._membership[table] = self._read_master(table)
        return self._membership[table]

    def exclusions(self) -> pd.DataFrame | None:
        """master/tradable_exclusions.parquet(tradingItemId, fromDate, toDate 포함, reason, preferredItemId). 없으면 None(경고 없음)."""
        if self._exclusions is None:
            p = self.paths["exclusions"]
            self._exclusions = self._read_master("exclusions") if p.exists() else pd.DataFrame(
                columns=["tradingItemId", "fromDate", "toDate", "reason", "preferredItemId"])
        return self._exclusions if len(self._exclusions) else None

    def _interval_mask(self, ids: np.ndarray, from_i8: np.ndarray, to_i8: np.ndarray, closed_right: bool) -> np.ndarray:
        """구간 목록 → bool 패널. from 포함, to 는 closed_right=True 면 포함(universe·lifecycle), False 면 미포함."""
        T, N = self.shape
        order = np.argsort(self._ids, kind="stable")
        sorted_ids = self._ids[order]
        pos = np.searchsorted(sorted_ids, ids)
        posc = np.minimum(pos, sorted_ids.size - 1)
        found = sorted_ids[posc] == ids
        col = order[posc[found]]
        f, t = from_i8[found], to_i8[found]
        f = np.where(f == INT64_MIN, self._dates_i8[0] - 1, f)          # NaT → 열림(앞)
        t = np.where(t == INT64_MIN, self._dates_i8[-1] + 1, t)         # NaT → 열림(뒤)
        s = np.searchsorted(self._dates_i8, f, side="left")
        e = np.searchsorted(self._dates_i8, t, side="right" if closed_right else "left")
        valid = e > s
        if col.size == np.unique(col).size:
            # id 당 구간 1개(lifecycle): 직접 비교(bool T×N 2개)
            lo = np.full(N, T, dtype=np.int64)
            hi = np.zeros(N, dtype=np.int64)
            lo[col[valid]] = s[valid]
            hi[col[valid]] = e[valid]
            rows = np.arange(T)[:, None]
            return (rows >= lo[None, :]) & (rows < hi[None, :])
        diff = np.zeros((T + 1, N), dtype=np.int16)
        np.add.at(diff, (s[valid], col[valid]), 1)
        np.add.at(diff, (e[valid], col[valid]), -1)
        return np.cumsum(diff[:-1], axis=0, dtype=np.int16) > 0

    def _mask_tradable(self, apply_lineage: bool) -> np.ndarray:
        lc = self.lifecycle()
        T, N = self.shape
        first = np.full(N, INT64_MIN, dtype=np.int64)
        last = np.full(N, INT64_MIN, dtype=np.int64)
        present = np.isin(self._ids, lc.index.to_numpy())
        missing = int((~present).sum())
        self.diagnostics["tradable_ids_missing_lifecycle"] = missing
        if missing and self.missing_lifecycle == "error":
            raise ContractError(f"{missing} panel ids missing from lifecycle: {self._ids[~present][:5].tolist()}")
        if missing and self.missing_lifecycle == "open":
            warnings.warn(f"{missing} panel ids missing from lifecycle → treated as open (tradable where close notna)",
                          stacklevel=3)
        sub = lc.reindex(self._ids[present])
        first[present] = _ts_i8(pd.to_datetime(sub["firstTradeDate"]).values)
        last[present] = _ts_i8(pd.to_datetime(sub["lastTradeDate"]).values)
        ids_use = self._ids[present] if self.missing_lifecycle == "exclude" else self._ids
        f_use = first[present] if self.missing_lifecycle == "exclude" else first
        l_use = last[present] if self.missing_lifecycle == "exclude" else last
        window = self._interval_mask(ids_use, f_use, l_use, closed_right=True)
        close = self.get_panel("close", "raw", as_array=True, cut=None)
        mask = window & ~np.isnan(close)
        ex = self.exclusions()
        if ex is not None:
            cut = self._interval_mask(ex["tradingItemId"].to_numpy(np.int64), _ts_i8(pd.to_datetime(ex["fromDate"]).values),
                                      _ts_i8(pd.to_datetime(ex["toDate"]).values), closed_right=True)
            self.diagnostics["tradable_excluded_cells"] = int((mask & cut).sum())
            self.diagnostics["tradable_exclusion_rows"] = int(len(ex))
            mask &= ~cut
        else:
            self.diagnostics["tradable_excluded_cells"] = 0
        if apply_lineage:
            ln = self.lineage()
            ln = ln[ln["relation"].isin(["rename", "relist"]) & ln["effectiveDate"].notna()]
            if len(ln):
                # newId 는 effectiveDate 전날까지 False(옛 id 가 그 구간을 대표)
                cut = self._interval_mask(ln["newId"].to_numpy(np.int64),
                                          np.full(len(ln), INT64_MIN, dtype=np.int64),
                                          _ts_i8(pd.to_datetime(ln["effectiveDate"]).values), closed_right=False)
                mask &= ~cut
        return mask

    def _mask_priced(self) -> np.ndarray:
        """priced = firstPriceDate ≤ d ≤ ciqLastCloseDate(양끝 포함, NaT=열림) ∧ close(raw) notna.
        이어붙인 과거·승계사 가격 꼬리를 포함한 '벤더 가격이 있는 구간'(워밍업용). tradable ⊆ priced."""
        lc = self.lifecycle()
        first_col = "firstPriceDate" if "firstPriceDate" in lc.columns else "firstTradeDate"
        last_col = "ciqLastCloseDate" if "ciqLastCloseDate" in lc.columns else "lastTradeDate"
        if first_col != "firstPriceDate" or last_col != "ciqLastCloseDate":
            warnings.warn(f"lifecycle lacks firstPriceDate/ciqLastCloseDate; priced uses {first_col}/{last_col}", stacklevel=3)
        N = self.shape[1]
        present = np.isin(self._ids, lc.index.to_numpy())
        sub = lc.reindex(self._ids[present])
        first = np.full(N, INT64_MIN, dtype=np.int64)
        last = np.full(N, INT64_MIN, dtype=np.int64)
        first[present] = _ts_i8(pd.to_datetime(sub[first_col]).values)
        last[present] = _ts_i8(pd.to_datetime(sub[last_col]).values)
        if self.missing_lifecycle == "exclude":
            window = self._interval_mask(self._ids[present], first[present], last[present], closed_right=True)
        else:
            window = self._interval_mask(self._ids, first, last, closed_right=True)
        return window & ~np.isnan(self.get_panel("close", "raw", as_array=True, cut=None))   # priced 는 꼬리 포함 → 미절단

    def _mask_universe(self, name: str, table: str = "membership") -> np.ndarray:
        mb = self.membership(table)
        sub = mb[mb["universe"] == name]
        if sub.empty:
            raise KeyError(f"universe '{name}' not in {table} (have {sorted(mb['universe'].unique())})")
        return self._interval_mask(sub["tradingItemId"].to_numpy(np.int64),
                                   _ts_i8(pd.to_datetime(sub["fromDate"]).values),
                                   _ts_i8(pd.to_datetime(sub["toDate"]).values), closed_right=True)

    def get_mask(self, kind="tradable", as_array: bool = False, apply_lineage: bool = False):
        """bool 패널. kind: 'tradable' | 'priced' | 'universe:<name>' | 'universe_v1rule:<name>'
        | 여러 개('&' 결합 문자열 또는 시퀀스) → 교집합.

        tradable = firstTradeDate ≤ d ≤ lastTradeDate(포함, NaT=열림) & close(raw) notna, 그리고
                   master/tradable_exclusions.parquet 의 (id, [fromDate, toDate]) 구간은 False(파일 없으면 그대로).
        priced   = firstPriceDate ≤ d ≤ ciqLastCloseDate & close(raw) notna (이어붙인 과거·가격 꼬리 포함, 워밍업용).
        apply_lineage=True 이면 lineage(rename/relist) 의 newId 를 effectiveDate 전까지 False(옵션).
        """
        kinds = kind.split("&") if isinstance(kind, str) else list(kind)
        kinds = [k.strip() for k in kinds if k.strip()]
        if not kinds:
            raise ValueError("empty mask kind")
        out = None
        for k in kinds:
            if k == "tradable":
                m = self._mask_tradable(apply_lineage)
            elif k == "priced":
                m = self._mask_priced()
            elif k.startswith("universe:"):
                m = self._mask_universe(k.split(":", 1)[1])
            elif k.startswith("universe_v1rule:"):
                m = self._mask_universe(k.split(":", 1)[1], table="membership_v1rule")
            elif k in UNIVERSE_NAMES:
                m = self._mask_universe(k)
            else:
                raise KeyError(f"unknown mask kind '{k}' (tradable | priced | universe:<name> | universe_v1rule:<name>)")
            out = m if out is None else (out & m)
        return self._wrap(out, as_array)

    def tradable_universe(self, name: str = "Top500", as_array: bool = False, apply_lineage: bool = False,
                          v1rule: bool = False):
        """tradable ∧ universe:<name>(v1rule=True 면 universe_v1rule:<name>) 헬퍼."""
        return self.get_mask(["tradable", f"{'universe_v1rule' if v1rule else 'universe'}:{name}"],
                             as_array=as_array, apply_lineage=apply_lineage)

    # ------------------------------------------------------------------------------------------------------
    # 그룹 패널(sector / industry) — build_group_panels.py 형식, 원천 master/classification_history
    # ------------------------------------------------------------------------------------------------------
    def classification_history(self) -> pd.DataFrame:
        """master/classification_history.parquet: tradingItemId, sector, simpleIndustryDescription, effectiveFrom,
        effectiveTo(NaT=현재; T1 데이터는 포함 경계), observedAt, source, historyQuality. 라벨은 strip, 빈 문자열 → None."""
        if self._classification is None:
            df = self._read_master("classification_history")
            for c in ("sector", "simpleIndustryDescription"):
                v = df[c].astype(object).map(lambda x: x.strip() if isinstance(x, str) else None)
                df[c] = v.where(v.notna() & (v != ""), None)
            df["effectiveFrom"] = pd.to_datetime(df["effectiveFrom"])
            df["effectiveTo"] = pd.to_datetime(df["effectiveTo"])
            if df["effectiveFrom"].isna().any():
                raise ContractError("classification_history.effectiveFrom contains NaT")
            both = df.dropna(subset=["sector", "simpleIndustryDescription"])
            spread = both.groupby("simpleIndustryDescription")["sector"].nunique()
            bad = spread[spread > 1]
            if len(bad):
                raise ContractError(f"industry nested in several sectors (group hierarchy broken): {bad.index.tolist()[:5]}")
            self._classification = df.sort_values(["tradingItemId", "effectiveFrom"], kind="stable").reset_index(drop=True)
        return self._classification

    def group_labels(self, sector_index=None, industry_index=None) -> dict[str, list[str]]:
        """코드→라벨 목록(코드 = 위치+1). sector: v1 sectorIx 순서(파일로 대체 가능; 목록 밖 라벨은 오류),
        industry: 이력 전체 라벨의 정렬 순(기존 industryIx 파일을 주면 그 순서 고정)."""
        if self._group_labels is None:
            ch = self.classification_history()
            out = {}
            for kind, (col, _) in GROUP_KINDS.items():
                present = sorted(ch[col].dropna().unique().tolist())
                index_path = sector_index if kind == "sector" else industry_index
                if index_path is not None:
                    ordered = pq.read_table(index_path).column(0).to_pylist()
                    if not ordered or ordered[0] is not None:
                        raise ContractError(f"{index_path} must start with a None row")
                    ordered = [str(v).strip() for v in ordered[1:]]
                elif kind == "sector":
                    ordered = list(SECTOR_ORDER_V1)
                else:
                    ordered = present
                unknown = [x for x in present if x not in ordered]
                if unknown:
                    raise ContractError(f"{kind} label(s) {unknown[:5]} not in the code table (v1 sectorIx order / given index file)")
                out[kind] = ordered
            self._group_labels = out
        return self._group_labels

    def get_group_panel(self, kind: str, mode: str = "broadcast_current", as_array: bool = False):
        """그룹 코드 wide float32(미분류 NaN). kind ∈ sector/industry, mode ∈
        'broadcast_current'(최신 분류를 전 기간 방송 = v1·build_group_panels 동일, 엔진 기본),
        'pit'(그 날짜에 유효한 분류만: effectiveFrom ≤ d ≤ effectiveTo(포함, NaT=열림); 관측 이전은 NaN).
        close 유무로 마스킹하지 않는다(정적 방송 규약). pit 는 관측 이전 구간이 전부 NaN 이라 엔진 기동 조건
        (종가 있는 bar 에 유한 코드 0 개면 실패)에 걸릴 수 있어 export 기본은 broadcast_current."""
        if kind not in GROUP_KINDS:
            raise KeyError(f"kind must be one of {tuple(GROUP_KINDS)}")
        if mode not in GROUP_MODES:
            raise ValueError(f"mode must be one of {GROUP_MODES}")
        key = (f"group:{kind}", mode)
        if key not in self._panels:
            t0 = time.perf_counter()
            col = GROUP_KINDS[kind][0]
            labels = self.group_labels()[kind]
            code_of = {lab: np.float32(i + 1) for i, lab in enumerate(labels)}
            ch = self.classification_history()
            T, N = self.shape
            out = np.full((T, N), np.nan, dtype=np.float32)
            order = np.argsort(self._ids, kind="stable")
            sorted_ids = self._ids[order]
            cid = ch["tradingItemId"].to_numpy(np.int64)
            pos = np.searchsorted(sorted_ids, cid)
            posc = np.minimum(pos, sorted_ids.size - 1)
            found = sorted_ids[posc] == cid
            colidx = np.where(found, order[posc], -1)
            codes = ch[col].map(lambda v: code_of.get(v, np.nan) if v is not None else np.nan).to_numpy(dtype=np.float32)
            f_i8 = _ts_i8(ch["effectiveFrom"].values)
            t_i8 = _ts_i8(ch["effectiveTo"].values)
            t_i8 = np.where(t_i8 == INT64_MIN, np.iinfo(np.int64).max, t_i8)
            keep = colidx >= 0
            colidx, codes, f_i8, t_i8 = colidx[keep], codes[keep], f_i8[keep], t_i8[keep]
            srt = np.lexsort((f_i8, colidx))
            colidx, codes, f_i8, t_i8 = colidx[srt], codes[srt], f_i8[srt], t_i8[srt]
            bounds = np.searchsorted(colidx, np.arange(N + 1))
            n_ids_classified = 0
            if mode == "broadcast_current":
                cur = np.full(N, np.nan, dtype=np.float32)
                for j in range(N):
                    a, b = bounds[j], bounds[j + 1]
                    if a == b:
                        continue
                    open_rows = np.flatnonzero(t_i8[a:b] == np.iinfo(np.int64).max)
                    r = a + (open_rows[-1] if open_rows.size else (b - a - 1))   # 열린(현재) 행, 없으면 마지막 effectiveFrom
                    cur[j] = codes[r]
                out[:] = cur[None, :]
                n_ids_classified = int(np.isfinite(cur).sum())
            else:
                D = self._dates_i8
                for j in range(N):
                    a, b = bounds[j], bounds[j + 1]
                    if a == b:
                        continue
                    pidx = np.searchsorted(f_i8[a:b], D, side="right") - 1
                    ok = pidx >= 0
                    pc = np.where(ok, pidx, 0)
                    ok &= D <= t_i8[a:b][pc]
                    col_codes = np.where(ok, codes[a:b][pc], np.nan).astype(np.float32)
                    out[:, j] = col_codes
                    n_ids_classified += int(np.isfinite(col_codes).any())
            self.diagnostics[f"group_{kind}_{mode}"] = {
                "ids_classified": n_ids_classified, "ids_total": int(N), "labels": len(labels),
                "history_rows_used": int(keep.sum()),
                "cells_finite": int(np.isfinite(out).sum()),
            }
            self._panels[key] = out
            self._timed(f"group:{kind}:{mode}", t0)
        return self._wrap(self._panels[key], as_array)

    def export_group_panels(self, out_dir, mode: str = "broadcast_current", sector_index=None, industry_index=None) -> dict:
        """엔진 `base.groups` 디렉토리 생성: sector.parquet·industry.parquet(wide float32, 축 = 이 패널의 달력·id 문자열 열),
        sectorIx.parquet·industryIx.parquet(0 행 None), groups_manifest.json. 파일 형식·쓰기 옵션은 build_group_panels.py 와 동일."""
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        labels = self.group_labels(sector_index, industry_index)
        dates, ids = self.dates(), self.ids()
        cols = [str(int(i)) for i in ids]
        files = {}
        for kind, (_, ix_col) in GROUP_KINDS.items():
            arr = self.get_group_panel(kind, mode, as_array=True)
            df = pd.DataFrame(arr, index=pd.DatetimeIndex(dates.values), columns=cols, copy=False)
            df.index.name = None
            path = out / f"{kind}.parquet"
            pq.write_table(pa.Table.from_pandas(df, preserve_index=True), path, **GROUP_WRITE_OPTIONS)
            files[f"{kind}.parquet"] = {"rows": int(arr.shape[0]), "cols": int(arr.shape[1]), "dtype": "float32",
                                        "cells_finite": int(np.isfinite(arr).sum()), "bytes": int(path.stat().st_size),
                                        "sha256": _sha256(path)}
            ix = pd.DataFrame({ix_col: pd.Series([None] + list(labels[kind]), dtype=object)})
            ix_path = out / f"{ix_col}.parquet"
            pq.write_table(pa.Table.from_pandas(ix, preserve_index=None), ix_path, **GROUP_WRITE_OPTIONS)
            files[f"{ix_col}.parquet"] = {"rows": len(labels[kind]) + 1, "bytes": int(ix_path.stat().st_size),
                                          "sha256": _sha256(ix_path)}
        manifest = {"mode": mode, "source": str(self.paths["classification_history"]), "n_dates": int(dates.size),
                    "n_ids": int(ids.size), "labels": {k: len(v) for k, v in labels.items()},
                    "sector_code_order": "v1 sectorIx" if sector_index is None else str(sector_index),
                    "diagnostics": {k: v for k, v in self.diagnostics.items() if k.startswith("group_")}, "files": files}
        with open(out / "groups_manifest.json", "w") as fh:
            json.dump(manifest, fh, indent=1)
        return manifest

    # ------------------------------------------------------------------------------------------------------
    # 기업행동 원장 export(엔진용, rc2 §3-3)
    # ------------------------------------------------------------------------------------------------------
    def export_ca_ledger(self, out_path, basis: str = "split_adj", ledger_path=None, cut: str | None = "lifecycle", events_path=None) -> dict:
        """events/ca_ledger.parquet(raw 단위, 구주 기준) → engine/ca_ledger_cache.parquet(split_adj 패널 단위).

        한 행 = 원장 행(eventType merger_cash/merger_stock/rename/relist 제외 — events 파일 담당). 단위 환산(sf = splitFactor,
        raw 1 주 = sf 현재 주):
          sfParentBefore        = 부모의 exDate **직전 거래일** 계수(구주 = 사건 직전 주식; 같은 날 분할이면 exDate 계수와 다름)
          cashPerShareCache     = cashPerShare / sfParentBefore
          childRatioCache       = childRatio × sf_child(exDate) / sfParentBefore   (현재 자회사 주 / 현재 부모 주)
          childCloseAtExCache   = 자회사 exDate split_adj 종가(패널 관측). 없고 원장이 ③④(event_value/vendor_implied/wi_close) 이면
                                  (childValuePerOldShare / childRatio) / sf_child(exDate) 로 환산(isEstimate 유지 → 엔진 when-issued fallback).
                                  자회사가 패널에 없으면 NaN(childRatioCache 도 NaN: distribution_unpriced).
          parentCloseAtExCache / parentCloseBeforeCache = 부모 split_adj 종가(exDate / 직전 관측), staleness 앵커
          parentFactorCache     = 1.0. quantity 행은 parentFactor == sfParentBefore/sf(exDate) 를 1e-9 로 확인, 아니면 ContractError
                                  (원장과 계수 테이블이 다른 캐시). 비-quantity 행은 parentFactor == 1 이어야 함.
        exDate 가 패널 밖(첫 날 이전·마지막 날 이후)이거나 부모가 패널에 없는 행은 버리고 개수만 기록, 패널 비거래일이면 다음 패널 날짜로 옮김.
        종료된 부모(lifecycle lastTradeDate < exDate)의 현금 leg 는 events 파일이 담당하므로 제외; 부모 마지막 관측 종가가 exDate 보다
        CA_EXPORT_STALE_BARS 넘게 오래된 행(앵커 stale)도 제외. 금액 없는 현금 leg·자회사 미확인 stock/rights leg 는 엔진이 book 할 수 없지만
        **표시용 행**으로 내보낸다(rc2f Y1): status 'unresolved', isEstimate False, cashPerShareCache/childRatioCache/childCloseAtExCache NaN,
        childTradingItemId null, 새 컬럼 markerReason ∈ {no_cash_amount, no_child, coacthist_stale_termination(D1: co_acthist 정지 뒤 조건 없는 종료, source lifecycle)}(그 밖의 행은 null). 자회사 id 가 없어도 ③④ 추정값이 있는
        (childValuePerOldShare 유한, status ≠ unresolved, childValueSource ∈ CA_ESTIMATE_SOURCES) stock/rights 행은 표시용 행이 아니라 값 leg 로 내보낸다
        (Y1-B): 새 컬럼 childValuePerShareCache = childValuePerOldShare / sfParentBefore(현재 부모 1주당 가치; 유한·양인 모든 값 leg 에 채움, 엔진 조건과 동일)로 엔진이 자회사
        없이 손익으로 기장한다. 새 컬럼 estimateReason(estimate_reason(): ratio/date/value 를 '+' 로 결합, 추정 표시 없는 행 null)은 시세로 평가되는 leg 라도
        무엇이 추정인지 엔진에 알려 손익 미확정으로 기록하게 한다(isEstimate 는 그대로). 다른 제외 규칙은 표시용 행에도 그대로
        적용되고 manifest['marker_rows'] 에 사유별 원장 수·export 수·다른 규칙으로 제외된 수를 기록한다. 표시용 행이 같은 export 키의 실제
        행과 겹치면 표시용 행만 버린다(marker_rows.dropped_key_conflict). export 키 (tradingItemId, exDate, legType, childTradingItemId) 중복은
        ContractError(빌드 실패).
        원장의 stitched_copy 행(exportExclude=true, 승계 id 의 이어붙인 과거에 복제된 사건)은 제외(stitched_copies_excluded).
        events 파일(events_path, 기본 root/events/corporate_actions.parquet)과 교차(E1 §9): 부모의 정산일(settlementDate) 이후 leg 중 events 행의
        승계사와 같은 자회사 stock leg, events 행에 현금 leg 가 있을 때의 cash leg 는 엔진이 events 로 처리하므로 제외(카운트
        events_successor_duplicate / events_cash_duplicate), 그 밖의 추가 자회사 leg(SLM 2번째 자회사, DISC.K, TROX)만 남긴다.
        isEstimate 는 childValueSource ∈ {event_value, vendor_implied} 또는 원장 isEstimate(비율 추정)일 때만 true(wi_close 는 false).
        childRatioRaw(원장 childRatio, 구주 기준) 를 함께 내보내 자회사가 패널 밖(childRatioCache NaN)이어도 비율 정보를 잃지 않게 한다.
        rights/warrant 행(E3): expiryDate 그대로, subscriptionPriceCache = subscriptionPrice / sf_child(exDate)(현재 자회사 1주당 행사가;
        자회사가 패널 밖이면 raw 그대로), exerciseRatioCache = exerciseRatio × sf_child(exDate)(권리 1개당 현재 자회사 주수; 패널 밖이면 raw).
        반환 manifest: rows, sha256, ledger_rows, ledger_sha256, dropped 카운트, 단위 환산 요약."""
        if basis != "split_adj":
            raise ValueError("export_ca_ledger supports basis='split_adj' only (engine panel basis)")
        if cut not in (None, "lifecycle"):
            raise ValueError("cut must be 'lifecycle' or None")
        ledger_path = Path(ledger_path) if ledger_path is not None else self.root / CA_LEDGER_REL
        if not ledger_path.exists():
            raise FileNotFoundError(ledger_path)
        t0 = time.perf_counter()
        L = pq.read_table(ledger_path).to_pandas()
        n_ledger = int(len(L))
        need = {"eventId", "tradingItemId", "exDate", "eventType", "legType", "cashPerShare", "parentFactor", "childTradingItemId",
                "childRatio", "childValueSource", "childValuePerOldShare", "payDate", "isEstimate", "status", "expiryDate", "subscriptionPrice", "exerciseRatio"}
        if not need <= set(L.columns):
            raise ContractError(f"ca_ledger lacks columns {sorted(need - set(L.columns))}")
        L = L[~L.eventType.isin(CA_EXPORT_EXCLUDED_TYPES)].reset_index(drop=True)
        n_excluded = n_ledger - len(L)
        # 이어붙인 과거의 복사본(source stitched_copy / exportExclude): 엔진은 거래 id 만 보유 → 제외
        excl = L["exportExclude"].fillna(False).astype(bool).to_numpy() if "exportExclude" in L.columns else np.zeros(len(L), dtype=bool)
        excl |= (L.source.astype(str) == "stitched_copy").to_numpy()
        n_stitched = int(excl.sum())
        L = L[~excl].reset_index(drop=True)
        # 표시용 행(Y1): 금액 없는 현금 leg / 자회사 미확인 stock·rights leg 는 제거하지 않고 markerReason 으로 표시해 끝까지 끌고 간다.
        # 자회사가 없어도 원장에 ③④ 추정값(childValuePerOldShare, status ≠ unresolved)이 있으면 표시용 행이 아니라 값 leg 다(Y1-B:
        # 엔진이 childValuePerShareCache 를 자회사 없이 손익으로 기장, returns_total flag 3 과 같은 경로).
        no_cash = ((L.legType == "cash") & ~np.isfinite(L.cashPerShare.to_numpy(np.float64))).to_numpy()
        _v = L.childValuePerOldShare.to_numpy(np.float64)
        valued = (np.isfinite(_v) & (_v > 0) & (L.status.astype(str) != "unresolved").to_numpy()      # 엔진 조건과 동일: 유한·양
                  & np.isin(L.childValueSource.astype(str).to_numpy(), CA_ESTIMATE_SOURCES))
        no_child = (L.legType.isin(["stock", "rights"]) & L.childTradingItemId.isna()).to_numpy() & ~valued
        # D1 (lead decision 2026-09-21): terminations after the co_acthist feed stopped (ledger source lifecycle, notes tag) are display-only rows
        # with their own reason so the engine/BUILD can count them; they are excluded from returns v2 (prices_returns.EXCLUDED_SOURCES)
        stale_term = no_child & (L.source.astype(str) == "lifecycle").to_numpy() & L.notes.astype(str).str.contains(CA_STALE_TERMINATION_TAG, regex=False).to_numpy()
        L["_markerReason"] = np.where(no_cash, "no_cash_amount", np.where(stale_term, "coacthist_stale_termination", np.where(no_child, "no_child", None)))
        marker_in_ledger = {r: int((L._markerReason == r).sum()) for r in CA_MARKER_REASONS}
        marker_raw_status = {r: L[L._markerReason == r].status.astype(str).value_counts().to_dict() for r in CA_MARKER_REASONS}
        # events 파일 교차(E1 §9): 정산일 이후 events 승계사 stock leg·events 현금 leg 중복 제외
        ev_path = Path(events_path) if events_path is not None else self.root / "events" / "corporate_actions.parquet"
        n_ev_succ = n_ev_cash = 0
        excluded_events_rows = []
        if ev_path.exists():
            ev = pq.read_table(ev_path, columns=["tradingItemId", "settlementDate", "endDate", "successorTradingItemId", "hasCashLeg", "hasStockLeg"]).to_pandas()
            ev = ev[ev.hasCashLeg.astype("boolean").fillna(False).astype(bool) | ev.hasStockLeg.astype("boolean").fillna(False).astype(bool)].copy()
            ev["settle"] = pd.to_datetime(ev.settlementDate).fillna(pd.to_datetime(ev.endDate))
            ev = ev.dropna(subset=["settle"]).sort_values("settle").drop_duplicates("tradingItemId", keep="last")
            m = L.merge(ev[["tradingItemId", "settle", "successorTradingItemId", "hasCashLeg"]], on="tradingItemId", how="left")
            after = m.settle.notna() & (pd.to_datetime(m.exDate) >= m.settle)
            # 표시용 행(child null)과 승계사의 비교는 <NA> 가 되므로 False 로 채운다(자회사 미확인 leg 는 승계사 중복이 아님)
            succ = (after & (m.legType == "stock") & m.successorTradingItemId.notna()
                    & (m.childTradingItemId.astype("Int64") == m.successorTradingItemId.astype("Int64"))).fillna(False).astype(bool)
            cash_dup = (after & (m.legType == "cash") & m.hasCashLeg.astype("boolean").fillna(False).astype(bool)).fillna(False).astype(bool)
            n_ev_succ, n_ev_cash = int(succ.sum()), int(cash_dup.sum())
            excluded_events_rows = m[succ | cash_dup][["eventId", "tradingItemId", "exDate", "legType", "childTradingItemId"]].astype(str).to_dict("records")
            L = L[~(succ | cash_dup).to_numpy()].reset_index(drop=True)
        n_terminated_cash = 0
        if self.paths["lifecycle"].exists():
            lc = self.lifecycle()
            last = pd.to_datetime(L.tradingItemId.map(lc["lastTradeDate"]))
            if "status" in lc.columns:
                last = last.where(L.tradingItemId.map(lc["status"]) != "active", pd.NaT)
            term_cash = (L.legType == "cash") & last.notna() & (pd.to_datetime(L.exDate) > last)
            n_terminated_cash = int(term_cash.sum())
            L = L[~term_cash].reset_index(drop=True)
        self._load_coords()
        ids, dates_i8 = self._ids, self._dates_i8
        T = dates_i8.size
        order = np.argsort(ids, kind="stable")
        sorted_ids = ids[order]

        def col_index(arr: np.ndarray) -> np.ndarray:
            pos = np.searchsorted(sorted_ids, arr)
            posc = np.minimum(pos, sorted_ids.size - 1)
            found = (sorted_ids[posc] == arr) & (arr >= 0)
            out = np.full(arr.size, -1, dtype=np.int64)
            out[found] = order[posc[found]]
            return out

        pid = L.tradingItemId.to_numpy(np.int64)
        ci = col_index(pid)
        ex_i8 = _ts_i8(pd.to_datetime(L.exDate).values)
        ri = np.searchsorted(dates_i8, ex_i8, side="left")            # 패널 비거래일 → 다음 패널 날짜
        in_panel = (ri < T) & (ex_i8 >= dates_i8[0])
        moved = in_panel & (dates_i8[np.minimum(ri, T - 1)] != ex_i8)
        keep = (ci >= 0) & in_panel
        dropped = {"excluded_event_types": n_excluded, "stitched_copies_excluded": n_stitched,
                   "events_successor_duplicate": n_ev_succ, "events_cash_duplicate": n_ev_cash,
                   "cash_leg_after_parent_lastTradeDate": n_terminated_cash,
                   "parent_not_in_panel": int((ci < 0).sum()), "exDate_outside_panel": int(((ci >= 0) & ~in_panel).sum())}
        L = L[keep].reset_index(drop=True)
        ci, ri, moved = ci[keep], ri[keep], moved[keep]
        # 구주 = 부모의 exDate 직전 **관측 종가일**(엔진 prev mark, R2 prevDate 와 동일)의 주식. 그 날이 없으면(첫 bar 이전) 적용 불가 → 버림
        close = self.get_panel("close", "split_adj", as_array=True, cut=cut)
        obs = np.where(np.isfinite(close), np.arange(T, dtype=np.int32)[:, None], np.int32(-1))
        last_obs = np.maximum.accumulate(obs, axis=0)
        del obs
        prev_ri = np.where(ri > 0, last_obs[np.maximum(ri - 1, 0), ci], -1)
        has_prev = prev_ri >= 0
        stale = has_prev & ((ri - prev_ri) > CA_EXPORT_STALE_BARS)          # 부모 마지막 관측이 exDate 보다 5 bar 넘게 이전(종료 뒤 stale 앵커)
        dropped["no_prior_parent_close"] = int((~has_prev).sum())
        dropped["parent_close_stale_before_exDate"] = int(stale.sum())
        keep2 = has_prev & ~stale
        L = L[keep2].reset_index(drop=True)
        ci, ri, moved, prev_ri = ci[keep2], ri[keep2], moved[keep2], prev_ri[keep2]
        ex_days = dates_i8[ri] // NS_PER_DAY
        prev_days = dates_i8[prev_ri] // NS_PER_DAY
        pid = L.tradingItemId.to_numpy(np.int64)
        tbl = self._factor_table("split")
        sf_ex = self._lookup(tbl, ci.astype(np.int32), ex_days, "split")
        sf_before = self._lookup(tbl, ci.astype(np.int32), prev_days, "split")
        p_ex = close[ri, ci]
        p_before = close[prev_ri, ci]
        # 자회사
        child = L.childTradingItemId.astype("Int64")
        cid = child.fillna(-1).to_numpy(np.int64)
        cci = col_index(cid)
        has_child = cid >= 0
        child_in_panel = has_child & (cci >= 0)
        sf_child = np.full(len(L), np.nan)
        c_close = np.full(len(L), np.nan)
        if child_in_panel.any():
            sf_child[child_in_panel] = self._lookup(tbl, cci[child_in_panel].astype(np.int32), ex_days[child_in_panel], "split")
            c_close[child_in_panel] = close[ri[child_in_panel], cci[child_in_panel]]
        ratio = L.childRatio.to_numpy(np.float64)
        value = L.childValuePerOldShare.to_numpy(np.float64)
        src = L.childValueSource.astype(str).to_numpy()
        is_est = L.isEstimate.fillna(False).to_numpy(dtype=bool)
        fallback = child_in_panel & ~np.isfinite(c_close) & np.isin(src, CA_ESTIMATE_SOURCES) & np.isfinite(value) & np.isfinite(ratio) & (ratio > 0)
        c_close = np.where(fallback, (value / np.where(ratio > 0, ratio, np.nan)) / sf_child, c_close)
        is_est = (is_est | np.isin(src, ("event_value", "vendor_implied"))) & (src != "wi_close")     # ② 검증된 when-issued 는 추정 아님
        child_ratio_cache = np.where(child_in_panel, ratio * sf_child / sf_before, np.nan)
        cash_cache = L.cashPerShare.to_numpy(np.float64) / sf_before
        # 값 leg 의 구주 1주당 가치를 현재 부모 1주당으로: 자회사가 패널 밖(또는 미확인)이어도 엔진이 ③④ 추정값을 손익으로 기장할 수 있다
        value_leg = L.legType.isin(["stock", "rights"]).to_numpy()
        child_value_cache = np.where(value_leg & np.isfinite(value) & (value > 0), value / sf_before, np.nan)
        # 표시용 행: 값 NaN·추정 아님·status unresolved (원장 status 가 estimated 여도 엔진에는 미해결로 전달)
        marker = L._markerReason.to_numpy(dtype=object)
        is_marker = np.array([m is not None for m in marker], dtype=bool)
        cash_cache = np.where(is_marker, np.nan, cash_cache)
        child_ratio_cache = np.where(is_marker, np.nan, child_ratio_cache)
        c_close = np.where(is_marker, np.nan, c_close)
        child_value_cache = np.where(is_marker, np.nan, child_value_cache)
        is_est = is_est & ~is_marker
        status_out = np.where(is_marker, "unresolved", L.status.astype(str).to_numpy())
        est_reason = estimate_reason(L).to_numpy(dtype=object)
        est_reason = np.where(is_marker, "", est_reason)                     # 표시용 행은 추정 아님
        # parentFactor 검사(split_adj 패널은 수량 변환을 이미 반영)
        pf = L.parentFactor.to_numpy(np.float64)
        implied = sf_before / sf_ex
        quantity = (L.legType == "quantity").to_numpy()
        bad_q = quantity & ~(np.abs(pf - implied) <= 1e-9 * np.maximum(1.0, np.abs(pf)))
        if bad_q.any():
            # 직전 관측일과 exDate 사이(무시세 구간)에 quantity 행이 여럿이면 곱이 계수 비를 재현해야 한다
            grp = pd.DataFrame({"pid": pid[quantity], "prev": prev_ri[quantity], "pf": pf[quantity]}).groupby(["pid", "prev"]).pf.prod()
            prod = grp.reindex(list(zip(pid[bad_q], prev_ri[bad_q]))).to_numpy()
            bad_q[np.flatnonzero(bad_q)[np.abs(prod - implied[bad_q]) <= 1e-9 * np.maximum(1.0, np.abs(prod))]] = False
        bad_nq = ~quantity & (pf != 1.0)
        if bad_q.any() or bad_nq.any():
            ex_rows = L[bad_q | bad_nq][["eventId", "parentFactor"]].head(5)
            raise ContractError(f"ca_ledger parentFactor disagrees with the split factor table (ledger built for another cache?): "
                                f"{int(bad_q.sum())} quantity rows, {int(bad_nq.sum())} non-quantity rows; e.g. {ex_rows.to_dict('records')} "
                                f"implied {implied[bad_q | bad_nq][:5].tolist()}")
        out = pd.DataFrame({
            "tradingItemId": [str(int(i)) for i in pid], "exDate": pd.DatetimeIndex(dates_i8[ri].view("datetime64[ns]")),
            "eventType": L.eventType.astype(str).to_numpy(), "legType": L.legType.astype(str).to_numpy(),
            "cashPerShareCache": cash_cache,
            "childTradingItemId": pd.array([str(int(c)) if c >= 0 else None for c in cid], dtype="string"),
            "childRatioCache": child_ratio_cache, "childCloseAtExCache": c_close,
            "parentCloseAtExCache": p_ex.astype(np.float64), "parentCloseBeforeCache": p_before.astype(np.float64),
            "parentFactorCache": np.ones(len(L)), "payDate": pd.to_datetime(L.payDate).values,
            "isEstimate": is_est, "status": status_out, "childValueSource": src, "eventId": L.eventId.astype(str).to_numpy(),
            "childRatioRaw": ratio,
            "expiryDate": pd.to_datetime(L.expiryDate).values,
            "subscriptionPriceCache": np.where(child_in_panel, L.subscriptionPrice.to_numpy(np.float64) / np.where(np.isfinite(sf_child), sf_child, np.nan), L.subscriptionPrice.to_numpy(np.float64)),
            "exerciseRatioCache": np.where(child_in_panel, L.exerciseRatio.to_numpy(np.float64) * np.where(np.isfinite(sf_child), sf_child, np.nan), L.exerciseRatio.to_numpy(np.float64)),
            "markerReason": pd.array([m if m is not None else None for m in marker], dtype="string"),
            "childValuePerShareCache": child_value_cache,
            "estimateReason": pd.array([r if r else None for r in est_reason], dtype="string"),
        })[CA_EXPORT_COLUMNS]
        schema = pa.schema([("tradingItemId", pa.string()), ("exDate", pa.timestamp("ns")), ("eventType", pa.string()), ("legType", pa.string()),
                            ("cashPerShareCache", pa.float64()), ("childTradingItemId", pa.string()), ("childRatioCache", pa.float64()),
                            ("childCloseAtExCache", pa.float64()), ("parentCloseAtExCache", pa.float64()), ("parentCloseBeforeCache", pa.float64()),
                            ("parentFactorCache", pa.float64()), ("payDate", pa.timestamp("ns")), ("isEstimate", pa.bool_()), ("status", pa.string()),
                            ("childValueSource", pa.string()), ("eventId", pa.string()), ("childRatioRaw", pa.float64()),
                            ("expiryDate", pa.timestamp("ns")), ("subscriptionPriceCache", pa.float64()), ("exerciseRatioCache", pa.float64()),
                            ("markerReason", pa.string()), ("childValuePerShareCache", pa.float64()), ("estimateReason", pa.string())])
        key_cols = ["tradingItemId", "exDate", "legType", "childTradingItemId"]
        # 표시용 행의 엔진 키 충돌: 실제 행과 같은 키(같은 날 금액 있는 현금 행 옆의 금액 없는 벤더 마커)면 실제 행이 정보를 담으므로 표시용 행을
        # 버리고, 표시용 행끼리만 겹치면(같은 날 자회사 미확인 leg 둘) 하나만 남긴다. 실제 행끼리의 중복은 여전히 ContractError.
        is_marker_out = out.markerReason.notna().to_numpy()
        key = pd.Series(list(zip(*(out[c].astype(str) for c in key_cols))), index=out.index)
        real_keys = set(key[~is_marker_out])
        key_conflict = is_marker_out & (key.isin(real_keys).to_numpy() | (key.duplicated(keep="first") & is_marker_out).to_numpy())
        conflict_by_reason = {r: int((key_conflict & (out.markerReason == r).to_numpy()).sum()) for r in CA_MARKER_REASONS}
        n_marker_key_conflict = int(key_conflict.sum())
        if n_marker_key_conflict:
            out = out[~key_conflict].reset_index(drop=True)
        dup = out.duplicated(key_cols, keep=False)
        if dup.any():                                   # 엔진 키 유일성: 위반 시 빌드 실패(리드 지시)
            raise ContractError(f"ca_ledger export has {int(dup.sum())} rows sharing (tradingItemId, exDate, legType, childTradingItemId): "
                                f"{out[dup][['eventId']].head(6).eventId.tolist()}")
        marker_exported = {r: int((out.markerReason == r).sum()) for r in CA_MARKER_REASONS}
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.Table.from_pandas(out, schema=schema, preserve_index=False), out_path, compression="zstd", use_dictionary=False)
        info = {
            "rows": int(len(out)), "bytes": int(out_path.stat().st_size), "sha256": _sha256(out_path), "basis": basis, "cut": cut,
            "ledger": str(ledger_path), "ledger_rows": n_ledger, "ledger_sha256": _sha256(ledger_path), "dropped": dropped,
            "exDate_moved_to_next_panel_date": int(moved.sum()),
            "rows_by_leg": out.legType.value_counts().to_dict(), "rows_by_status": out.status.value_counts().to_dict(),
            "child_rows": int(has_child.sum()), "child_in_panel": int(child_in_panel.sum()),
            "child_close_observed": int((child_in_panel & np.isfinite(c_close) & ~fallback).sum()),
            "child_close_estimated_fallback": int(fallback.sum()),
            "child_close_nan": int((has_child & ~np.isfinite(c_close)).sum()),
            "value_legs_without_panel_child": int((value_leg & ~child_in_panel & np.isfinite(child_value_cache)).sum()),
            "value_legs_without_panel_child_by_source": pd.Series(src[value_leg & ~child_in_panel & np.isfinite(child_value_cache)]).value_counts().to_dict(),
            "value_legs_no_child_id_valued": int((value_leg & ~has_child & np.isfinite(child_value_cache)).sum()),
            "estimate_reason_by_leg": {lt: pd.Series(est_reason[(L.legType.astype(str) == lt).to_numpy() & (est_reason != "")]).value_counts().to_dict()
                                       for lt in ("cash", "stock", "rights", "quantity")},
            "estimate_flagged_without_reason": int(((is_est | (status_out == "estimated")) & (est_reason == "")).sum()),
            "quoted_child_estimates": int((value_leg & child_in_panel & np.isfinite(c_close) & ~fallback & is_est).sum()),
            "value_legs_on_same_day_split": int((~quantity & (implied != 1.0)).sum()),
            "rights_rows": int((L.legType == "rights").sum()), "events_file": str(ev_path) if ev_path.exists() else None,
            "events_excluded_rows": excluded_events_rows[:200],
            "marker_rows": {"exported": int(sum(marker_exported.values())), "by_reason": marker_exported, "in_ledger_by_reason": marker_in_ledger,
                            "excluded_by_other_rules_by_reason": {r: marker_in_ledger[r] - marker_exported[r] - conflict_by_reason[r] for r in CA_MARKER_REASONS},
                            "dropped_key_conflict": n_marker_key_conflict, "dropped_key_conflict_by_reason": conflict_by_reason,
                            "ledger_status_by_reason": marker_raw_status,
                            "export_status": "unresolved", "export_values": "cashPerShareCache/childRatioCache/childCloseAtExCache NaN, isEstimate False"},
            "unit_conversion": {"childRatioRaw": "ledger childRatio (per pre-event share, raw units)", "cashPerShareCache": "cashPerShare / sf_parent(prev trading day)",
                                "subscriptionPriceCache": "subscriptionPrice / sf_child(exDate) when the child is in the panel, else raw",
                                "exerciseRatioCache": "exerciseRatio x sf_child(exDate) when the child is in the panel, else raw",
                                "childRatioCache": "childRatio x sf_child(exDate) / sf_parent(prev trading day)",
                                "childCloseAtExCache": "child split_adj close at exDate; else (childValuePerOldShare/childRatio)/sf_child for estimate sources",
                                "childValuePerShareCache": "childValuePerOldShare / sf_parent(prev trading day): value of the leg per current parent share; the engine books it as P&L when the child is not in its panel (P10 (3)(4))"},
            "seconds": round(time.perf_counter() - t0, 3),
        }
        self.diagnostics["ca_ledger_export"] = info
        return info

    # ------------------------------------------------------------------------------------------------------
    # events(정산) 파일 export(엔진용, rc2f Y1-A)
    # ------------------------------------------------------------------------------------------------------
    def export_events(self, out_path, basis: str = "split_adj", events_path=None, cut: str | None = "lifecycle") -> dict:
        """events/corporate_actions.parquet(raw 단위, k≡1) → engine/events_cache.parquet(split_adj 패널 단위, SPEC_loader §7).

        엔진(`events:` 블록, CLAUDE.md missing-close 문단)은 종료일 다음 첫 bar 이후 **대상의 첫 무시세 bar**(t) 에 정산하고 대상을 마지막
        관측 종가(mark, t 직전)로 평가하며, 파일의 앵커 `targetCacheMark@targetCacheMarkDate`(mark) 와 `successorCacheClose@successorCacheCloseDate`
        (t 의 승계사 종가)를 패널과 1e-4 로 대조한다. 이 함수는 그 규칙을 export 패널(basis, cut)에서 그대로 재현해 앵커를 다시 쓰고
        조건을 환산한다(sf = splitFactor, raw 1 주 = sf 현재 주):
          cashPerShareCache    = cashPerShareCache_raw / sf_T(mark 날짜)
          successorRatioCache  = successorRatioCache_raw × sf_S(t) / sf_T(mark 날짜)
          targetCacheMark      = 대상 split_adj 종가(mark), successorCacheClose = 승계사 split_adj 종가(t)
          cacheUnitFactorTarget = sf_T, cacheUnitFactorSuccessor = sf_S (Date 컬럼은 각각 mark·t)
        원본의 수락 판정은 존중한다(원본 cache 컬럼이 NaN 인 행 — no_terms·last_close·rejected:* — 은 NaN 유지, cacheUnitStatusRaw 에 원본 status).
        cacheUnitStatus: ok(환산·앵커 완료) / target_not_in_panel / no_mark_in_panel / no_settlement_bar_in_panel(대상이 패널 끝까지 시세 →
        엔진이 정산하지 않음; 비율 NaN) / successor_not_in_panel / no_successor_close_at_settlement(비율 NaN → 엔진은 settlementReturn·기본값)
        / no_terms / last_close / rejected:*. settlementBarDate = 엔진이 정산할 bar(없으면 NaT). 그 밖의 컬럼은 원본 그대로. 반환 manifest."""
        if basis != "split_adj":
            raise ValueError("export_events supports basis='split_adj' only (engine panel basis)")
        if cut not in (None, "lifecycle"):
            raise ValueError("cut must be 'lifecycle' or None")
        ev_path = Path(events_path) if events_path is not None else self.root / EVENTS_REL
        if not ev_path.exists():
            raise FileNotFoundError(ev_path)
        t0 = time.perf_counter()
        ev = pq.read_table(ev_path).to_pandas()
        need = {"tradingItemId", "endDate", "successorTradingItemId", "cashPerShareCache", "successorRatioCache"}
        if not need <= set(ev.columns):
            raise ContractError(f"events file lacks columns {sorted(need - set(ev.columns))}")
        n = len(ev)
        self._load_coords()
        ids, dates_i8 = self._ids, self._dates_i8
        T = dates_i8.size
        order = np.argsort(ids, kind="stable")
        sorted_ids = ids[order]

        def col_index(arr: np.ndarray) -> np.ndarray:
            pos = np.searchsorted(sorted_ids, arr)
            posc = np.minimum(pos, sorted_ids.size - 1)
            found = (sorted_ids[posc] == arr) & (arr >= 0)
            out = np.full(arr.size, -1, dtype=np.int64)
            out[found] = order[posc[found]]
            return out

        close = self.get_panel("close", basis, as_array=True, cut=cut)
        obs = np.isfinite(close) & (close > 0)
        tid = ev.tradingItemId.to_numpy(np.int64)
        ci = col_index(tid)
        succ = pd.to_numeric(ev.successorTradingItemId, errors="coerce").fillna(-1).to_numpy(np.int64)
        sci = col_index(succ)
        end_i8 = _ts_i8(pd.to_datetime(ev.endDate).values)
        end_day = (end_i8 // NS_PER_DAY) * NS_PER_DAY
        open_bar = np.searchsorted(dates_i8, end_day, side="right")          # 종료일 다음 첫 bar (엔진 upper_bound)
        cash_raw = ev.cashPerShareCache.to_numpy(np.float64)
        ratio_raw = ev.successorRatioCache.to_numpy(np.float64)
        raw_status = ev["cacheUnitStatus"].astype(str).to_numpy() if "cacheUnitStatus" in ev.columns else np.full(n, "", dtype=object)
        settle_bar = np.full(n, -1, dtype=np.int64)
        mark_bar = np.full(n, -1, dtype=np.int64)
        status = np.full(n, "", dtype=object)
        for i in range(n):
            c = int(ci[i])
            if c < 0:
                status[i] = "target_not_in_panel"
                continue
            col = obs[:, c]
            o = int(open_bar[i])
            t = -1
            if o < T:
                silent = np.flatnonzero(~col[o:])
                if silent.size:
                    t = o + int(silent[0])
            settle_bar[i] = t
            observed = np.flatnonzero(col[:t] if t >= 0 else col)
            if not observed.size:
                status[i] = "no_mark_in_panel"
                continue
            mark_bar[i] = int(observed[-1])
        has_mark = mark_bar >= 0
        tbl = self._factor_table("split")
        mark_days = dates_i8[np.maximum(mark_bar, 0)] // NS_PER_DAY
        sf_t = np.full(n, np.nan)
        sf_t[has_mark] = self._lookup(tbl, ci[has_mark].astype(np.int32), mark_days[has_mark], "split")
        cash_adj = np.where(has_mark & np.isfinite(cash_raw), cash_raw / sf_t, np.nan)
        target_mark = np.where(has_mark, close[np.maximum(mark_bar, 0), np.maximum(ci, 0)], np.nan).astype(np.float64)
        target_mark_date = np.where(has_mark, dates_i8[np.maximum(mark_bar, 0)], INT64_MIN)
        # 승계사: 정산 bar 의 split_adj 종가가 있어야 비율을 환산·앵커할 수 있다
        has_t = settle_bar >= 0
        succ_ok = has_mark & has_t & (sci >= 0)
        succ_close = np.full(n, np.nan)
        succ_close[succ_ok] = close[settle_bar[succ_ok], sci[succ_ok]]
        succ_quoted = succ_ok & np.isfinite(succ_close) & (succ_close > 0)
        sf_s = np.full(n, np.nan)
        if succ_quoted.any():
            sf_s[succ_quoted] = self._lookup(tbl, sci[succ_quoted].astype(np.int32), dates_i8[settle_bar[succ_quoted]] // NS_PER_DAY, "split")
        ratio_adj = np.where(succ_quoted & np.isfinite(ratio_raw), ratio_raw * sf_s / sf_t, np.nan)
        succ_close_out = np.where(succ_quoted, succ_close, np.nan)
        succ_close_date = np.where(succ_quoted, dates_i8[np.maximum(settle_bar, 0)], INT64_MIN)
        # 상태
        for i in range(n):
            if status[i]:
                continue
            if not np.isfinite(cash_raw[i]) and not np.isfinite(ratio_raw[i]):
                status[i] = raw_status[i] if raw_status[i].startswith("rejected:") or raw_status[i] == "last_close" else "no_terms"
            elif not has_t[i]:
                status[i] = "no_settlement_bar_in_panel"          # 조건은 환산돼 있지만 이 패널에서는 정산되지 않는다
            elif np.isfinite(ratio_raw[i]) and not succ_quoted[i]:
                status[i] = "successor_not_in_panel" if sci[i] < 0 else "no_successor_close_at_settlement"
            else:
                status[i] = "ok"
        out = ev.copy()
        out["cashPerShareCache"] = cash_adj
        out["successorRatioCache"] = ratio_adj
        out["targetCacheMark"] = target_mark
        out["targetCacheMarkDate"] = pd.to_datetime(np.where(has_mark, target_mark_date, np.datetime64("NaT").astype("int64")).view("datetime64[ns]"))
        out.loc[~has_mark, "targetCacheMarkDate"] = pd.NaT
        out["successorCacheClose"] = succ_close_out
        out["successorCacheCloseDate"] = pd.to_datetime(succ_close_date.view("datetime64[ns]"))
        out.loc[~succ_quoted, "successorCacheCloseDate"] = pd.NaT
        out["cacheUnitFactorTarget"] = sf_t
        out["cacheUnitFactorTargetDate"] = out["targetCacheMarkDate"]
        out["cacheUnitFactorSuccessor"] = sf_s
        out["cacheUnitStatusRaw"] = raw_status
        out["cacheUnitStatus"] = status.astype(str)
        out["cacheBasis"] = basis
        out["settlementBarDate"] = pd.to_datetime(np.where(has_t, dates_i8[np.maximum(settle_bar, 0)], 0).view("datetime64[ns]"))
        out.loc[~has_t, "settlementBarDate"] = pd.NaT
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.Table.from_pandas(out, preserve_index=False), out_path, compression="zstd", use_dictionary=False)
        restated_cash = has_mark & np.isfinite(cash_raw)
        info = {
            "rows": int(n), "bytes": int(out_path.stat().st_size), "sha256": _sha256(out_path), "basis": basis, "cut": cut,
            "events": str(ev_path), "events_sha256": _sha256(ev_path),
            "rows_target_in_panel": int((ci >= 0).sum()), "rows_with_settlement_bar": int(has_t.sum()), "rows_with_mark": int(has_mark.sum()),
            "cash_rows_restated": int(restated_cash.sum()), "ratio_rows_restated": int(np.isfinite(ratio_adj).sum()),
            "ratio_rows_dropped": int((np.isfinite(ratio_raw) & ~np.isfinite(ratio_adj)).sum()),
            "sf_target_ne_1": int(((restated_cash | np.isfinite(ratio_adj)) & np.isfinite(sf_t) & (np.abs(sf_t - 1.0) > 1e-12)).sum()),
            "sf_successor_ne_1": int((np.isfinite(ratio_adj) & (np.abs(sf_s - 1.0) > 1e-12)).sum()),
            "status": pd.Series(status).value_counts().to_dict(), "status_raw": pd.Series(raw_status).value_counts().to_dict(),
            "unit_conversion": {"cashPerShareCache": "cashPerShareCache_raw / sf_target(mark date)",
                                "successorRatioCache": "successorRatioCache_raw x sf_successor(settlement bar) / sf_target(mark date)",
                                "targetCacheMark": "target split_adj close on the mark bar (last observed close before the settlement bar)",
                                "successorCacheClose": "successor split_adj close on the settlement bar (first silent target bar after endDate)"},
            "seconds": round(time.perf_counter() - t0, 3),
        }
        self.diagnostics["events_export"] = info
        return info

    # ------------------------------------------------------------------------------------------------------
    # v1 스타일 wide export
    # ------------------------------------------------------------------------------------------------------
    @staticmethod
    def _write_wide(arr: np.ndarray, dates: pd.DatetimeIndex, ids: np.ndarray, path: Path,
                    compression: str, row_group_size: int) -> dict:
        path.parent.mkdir(parents=True, exist_ok=True)
        df = pd.DataFrame(arr, index=pd.DatetimeIndex(dates.values), columns=[str(int(i)) for i in ids], copy=False)
        df.index.name = None                      # → parquet 컬럼 '__index_level_0__' (v1·C++ 기본 time_col)
        table = pa.Table.from_pandas(df, preserve_index=True)
        pq.write_table(table, path, compression=compression, row_group_size=row_group_size, use_dictionary=False)
        return {"rows": int(arr.shape[0]), "cols": int(arr.shape[1]), "dtype": str(arr.dtype),
                "bytes": int(path.stat().st_size), "sha256": _sha256(path)}

    @staticmethod
    def mask_file_name(kind: str) -> str:
        return kind.replace("universe:", "").replace("&", "_").replace(":", "_")

    def export_wide(self, out_dir, fields: Sequence[str] = FIELDS, basis: str = "split_adj",
                    returns: bool = True, masks: Sequence[str] = ("tradable",), universes: Sequence[str] = (),
                    universes_v1rule: Sequence[str] = (), factors: bool = False, v1_names: bool = True,
                    compression: str = "zstd", row_group_size: int = 1024, dtype=None, release: bool = False,
                    groups: bool = False, groups_mode: str = "broadcast_current", groups_dir: str = "groups",
                    cut: str | None = "lifecycle") -> dict:
        """v1 레이아웃 export: BaseData/<field>.parquet(열=str(id), index=date), Returns/returns_total.parquet,
        Mask/<kind>.parquet(bool), Universe/<name>.parquet(bool), Universe_v1rule/<name>.parquet(bool),
        Factor/{splitFactor,divAdjFactor}.parquet. fields 에 'marketCap_USD' 를 넣으면 BaseData/marketCap_USD.parquet(v1 이름, raw).
        groups=True 면 <out>/<groups_dir>/{sector,industry,sectorIx,industryIx}.parquet 을 엔진 `base.groups` 형식으로 출력
        (기본 broadcast_current; 엔진 YAML: base: {cache: <out>, groups: <out>/groups}).
        cut='lifecycle'(기본): BaseData 필드·Returns 는 lifecycle.lastTradeDate 이후 셀을 NaN 으로 절단한 **엔진 입력 절단본**
        (events 가 절단 패널 기준으로 생성됨). cut=None 은 미절단(연구용). 절단 셀 수는 manifest['cut'] 에 기록.
        root/events/ca_ledger.parquet 이 있고 basis='split_adj' 면 engine/ca_ledger_cache.parquet(export_ca_ledger, rc2 §3-3) 도 함께 쓰고
        manifest['ca_ledger'] 에 원장 행 수·sha256·단위 환산 요약을 기록한다.
        EXPORT.json 에 축·산식·파일 sha256 을 기록한다. 반환값 = 그 manifest. release=True 면 필드별 패널을 쓴 뒤 해제(메모리)."""
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        dates, ids = self.dates(), self.ids()
        manifest = {
            "layout": "v1_wide", "basis": basis, "index_column": "__index_level_0__", "column_names": "str(tradingItemId)",
            "n_dates": int(dates.size), "n_ids": int(ids.size), "first_date": str(dates[0].date()),
            "last_date": str(dates[-1].date()), "source_root": str(self.root), "files": {},
            "cut": {"mode": cut, "cells_removed": {}},
        }
        if cut not in (None, "lifecycle"):
            raise ValueError("cut must be 'lifecycle' or None")
        t0 = time.perf_counter()
        for f in fields:
            f_basis = "raw" if f in EXTRA_FIELDS or f in FLAG_FIELDS else basis
            arr = self.get_panel(f, f_basis, as_array=True, cut=cut)
            if cut:
                manifest["cut"]["cells_removed"][f] = self.diagnostics.get("cut_cells", {}).get(f"{f}:{f_basis}", 0)
            if dtype is not None:
                arr = arr.astype(dtype)
            name = V1_FILE_NAMES.get(f, f) if v1_names else f
            rel = f"BaseData/{name}.parquet"
            manifest["files"][rel] = self._write_wide(arr, dates, ids, out / rel, compression, row_group_size)
            if release:
                del arr
                self._panels.pop((f, f_basis), None)
                self._panels.pop((f, f_basis, "cut"), None)
        if returns:
            rel = "Returns/returns_total.parquet"
            manifest["files"][rel] = self._write_wide(self.get_returns(as_array=True, cut=cut), dates, ids, out / rel,
                                                      compression, row_group_size)
            if cut:
                manifest["cut"]["cells_removed"]["returns_total"] = self.diagnostics.get("cut_cells", {}).get("returns_total", 0)
            if release:
                self._returns = None
        for k in masks:
            rel = f"Mask/{self.mask_file_name(k)}.parquet"
            manifest["files"][rel] = self._write_wide(self.get_mask(k, as_array=True), dates, ids, out / rel,
                                                      compression, row_group_size)
        for u in universes:
            rel = f"Universe/{u}.parquet"
            manifest["files"][rel] = self._write_wide(self._mask_universe(u), dates, ids, out / rel,
                                                      compression, row_group_size)
        for u in universes_v1rule:
            rel = f"Universe_v1rule/{u}.parquet"
            manifest["files"][rel] = self._write_wide(self._mask_universe(u, table="membership_v1rule"), dates, ids,
                                                      out / rel, compression, row_group_size)
        if factors:
            for name, fn in (("split", "splitFactor"), ("dividend", "divAdjFactor")):
                rel = f"Factor/{fn}.parquet"
                manifest["files"][rel] = self._write_wide(self.get_factor(name, as_array=True), dates, ids, out / rel,
                                                          compression, row_group_size)
        if groups:
            gm = self.export_group_panels(out / groups_dir, mode=groups_mode)
            for rel, info in gm["files"].items():
                manifest["files"][f"{groups_dir}/{rel}"] = info
            manifest["groups"] = {k: v for k, v in gm.items() if k != "files"}
        ledger_path = self.root / CA_LEDGER_REL
        if ledger_path.exists():
            if basis == "split_adj":
                ca = self.export_ca_ledger(out / CA_EXPORT_REL, basis=basis, ledger_path=ledger_path, cut=cut)
                manifest["files"][CA_EXPORT_REL] = {"rows": ca["rows"], "bytes": ca["bytes"], "sha256": ca["sha256"]}
                manifest["ca_ledger"] = {k: v for k, v in ca.items() if k not in ("rows", "bytes", "sha256")}
            else:
                manifest["ca_ledger"] = {"skipped": f"basis={basis} (ca_ledger export is defined for split_adj only)", "ledger": str(ledger_path)}
        events_path = self.root / EVENTS_REL
        if events_path.exists():
            if basis == "split_adj":
                evm = self.export_events(out / EVENTS_EXPORT_REL, basis=basis, events_path=events_path, cut=cut)
                manifest["files"][EVENTS_EXPORT_REL] = {"rows": evm["rows"], "bytes": evm["bytes"], "sha256": evm["sha256"]}
                manifest["events_cache"] = {k: v for k, v in evm.items() if k not in ("rows", "bytes", "sha256")}
                with open(out / "engine" / "events_cache_manifest.json", "w") as fh:
                    json.dump(evm, fh, indent=1, default=str)
            else:
                manifest["events_cache"] = {"skipped": f"basis={basis} (events export is defined for split_adj only)", "events": str(events_path)}
        if cut:
            manifest["cut"].update({k: v for k, v in self.diagnostics.get("cut", {}).items() if k != "mode"})
            manifest["cut"]["cells_removed_total"] = int(sum(manifest["cut"]["cells_removed"].values()))
        manifest["seconds"] = round(time.perf_counter() - t0, 3)
        manifest["ids"] = [int(i) for i in ids] if ids.size <= 100 else None
        with open(out / "EXPORT.json", "w") as fh:
            json.dump(manifest, fh, indent=1)
        return manifest

    @staticmethod
    def read_wide(path) -> pd.DataFrame:
        """export_wide 산출물(또는 v1 wide) 읽기: index=DatetimeIndex, columns=str(id)."""
        df = pd.read_parquet(path)
        df.index = pd.DatetimeIndex(df.index)
        return df

    @staticmethod
    def compare_wide(path_a, path_b, rtol: float = 0.0) -> dict:
        """parity 비교: 두 wide parquet 을 (date, symbol) 셀 단위로 대조. rtol=0 은 비트 동일(NaN 은 NaN 과 같음).

        반환: 공통 셀 수·불일치 수·최대 상대차·한쪽에만 있는 날짜/심볼 수."""
        a = CacheV21.read_wide(path_a)
        b = CacheV21.read_wide(path_b)
        a.columns = [str(c) for c in a.columns]
        b.columns = [str(c) for c in b.columns]
        dates = a.index.intersection(b.index)
        cols = a.columns.intersection(b.columns)
        x = a.loc[dates, cols].to_numpy()
        y = b.loc[dates, cols].to_numpy()
        if x.dtype == bool or y.dtype == bool:
            x = x.astype(np.float64)
            y = y.astype(np.float64)
        nan_both = np.isnan(x) & np.isnan(y)
        both = ~np.isnan(x) & ~np.isnan(y)
        if rtol == 0.0:
            same = (x == y) | nan_both
        else:
            same = nan_both | (both & (np.abs(x - y) <= rtol * np.maximum(np.abs(x), np.abs(y))))
        diff = np.abs(x[both] - y[both]) / np.maximum(np.maximum(np.abs(x[both]), np.abs(y[both])), 1e-300)
        bad = np.argwhere(~same)
        return {
            "dates_common": int(len(dates)), "dates_only_a": int(len(a.index.difference(b.index))),
            "dates_only_b": int(len(b.index.difference(a.index))), "symbols_common": int(len(cols)),
            "symbols_only_a": int(len(a.columns.difference(b.columns))), "symbols_only_b": int(len(b.columns.difference(a.columns))),
            "cells": int(same.size), "cells_mismatch": int((~same).sum()),
            "cells_nan_one_side": int((np.isnan(x) ^ np.isnan(y)).sum()),
            "max_rel_diff": float(diff.max()) if diff.size else 0.0,
            "examples": [(str(dates[i].date()), cols[j], float(x[i, j]), float(y[i, j])) for i, j in bad[:5]],
        }


# ----------------------------------------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------------------------------------
def _cli_info(cv: CacheV21) -> None:
    T, N = cv.shape
    print(f"root={cv.root}\nmaster_root={cv.master_root}\nshape T×N = {T} × {N}")
    print(f"dates {cv.dates()[0].date()} .. {cv.dates()[-1].date()}  ids[:5]={cv.ids()[:5].tolist()}")
    print("available fields:", cv.available_fields)
    print("diagnostics:", cv.diagnostics)
    for name in ("split", "dividend"):
        if cv.paths[name].exists():
            print(name, cv.validate_factors(strict=False)[name])


def _cli_bench(cv: CacheV21, out: Path | None, cut: str | None = "lifecycle") -> dict:
    """실데이터 측정: 좌표 → close split_adj → returns wide → export 6 필드."""
    rows = []

    def step(label, fn):
        t0 = time.perf_counter()
        r = fn()
        dt = time.perf_counter() - t0
        rows.append({"step": label, "seconds": round(dt, 2), "peak_rss_mb": round(_peak_rss_mb(), 0)})
        print(f"{label:40s} {dt:8.2f}s  peakRSS {_peak_rss_mb():8.0f} MB", flush=True)
        return r

    step("coords (dates/ids/row index)", cv._load_coords)
    step("validate_factors", lambda: cv.validate_factors(strict=True))
    step("close raw panel (uncut)", lambda: cv.get_panel("close", "raw", cut=None))
    step("close split_adj panel (uncut)", lambda: cv.get_panel("close", "split_adj", cut=None))
    step("close div_adj panel (uncut)", lambda: cv.get_panel("close", "div_adj", cut=None))
    if cut and cv.paths["lifecycle"].exists():
        step("close split_adj lifecycle cut", lambda: cv.get_panel("close", "split_adj", cut="lifecycle"))
        print("cut:", cv.diagnostics.get("cut"), "cells removed:", cv.diagnostics.get("cut_cells"))
    if cv.paths["returns_total"].exists():
        step("returns_total wide", cv.get_returns)
        chk = step("check_returns (recompute)", lambda: cv.check_returns(atol=1e-9))
        print("check_returns:", chk)
    if cv.paths["lifecycle"].exists():
        step("mask tradable (+exclusions)", lambda: cv.get_mask("tradable"))
        step("mask priced", lambda: cv.get_mask("priced"))
    if cv.paths["marketcap"].exists():
        step("marketCap_USD panel", lambda: cv.get_panel("marketCap_USD"))
    if cv.paths["membership"].exists():
        step("mask universe:Top500", lambda: cv.get_mask("universe:Top500"))
    if cv.paths["classification_history"].exists():
        step("group sector broadcast_current", lambda: cv.get_group_panel("sector", "broadcast_current"))
        step("group sector pit", lambda: cv.get_group_panel("sector", "pit"))
        step("group industry broadcast_current", lambda: cv.get_group_panel("industry", "broadcast_current"))
        print("groups:", {k: v for k, v in cv.diagnostics.items() if k.startswith("group_")})
    if out is not None:
        fields = [f for f in FIELDS if f in cv.available_fields] + (["marketCap_USD"] if "marketCap_USD" in cv.available_fields else [])
        m = step(f"export_wide {len(fields)} fields split_adj (+returns, masks)",
                 lambda: cv.export_wide(out, fields=fields, basis="split_adj", returns=cv.paths["returns_total"].exists(),
                                        masks=("tradable", "priced") if cv.paths["lifecycle"].exists() else (),
                                        universes=("Top500",) if cv.paths["membership"].exists() else (), release=True,
                                        groups=cv.paths["classification_history"].exists(), cut=cut))
        print("export cut:", m.get("cut"))
        total = sum(v["bytes"] for v in m["files"].values())
        print(f"export bytes total {total / 1e6:.1f} MB")
        rows.append({"step": "export bytes (MB)", "seconds": round(total / 1e6, 1), "peak_rss_mb": round(_peak_rss_mb(), 0)})
    res = {"shape": cv.shape, "steps": rows, "timings": {k: round(v, 3) for k, v in cv.timings.items()},
           "diagnostics": cv.diagnostics}
    print(json.dumps(res, indent=1, default=str))
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="cache_v2.1 reference loader")
    ap.add_argument("cmd", choices=["info", "export", "bench", "compare"])
    ap.add_argument("--root", default=None)
    ap.add_argument("--a", default=None, help="compare: wide parquet A")
    ap.add_argument("--b", default=None, help="compare: wide parquet B")
    ap.add_argument("--rtol", type=float, default=0.0)
    ap.add_argument("--master-root", default=None)
    ap.add_argument("--universe-root", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--fields", default=",".join(FIELDS))
    ap.add_argument("--basis", default="split_adj", choices=BASES)
    ap.add_argument("--masks", default="tradable")
    ap.add_argument("--universes", default="")
    ap.add_argument("--groups", action="store_true", help="export: groups/{sector,industry}.parquet (broadcast_current)")
    ap.add_argument("--groups-mode", default="broadcast_current", choices=GROUP_MODES)
    ap.add_argument("--no-cut", action="store_true", help="export/bench: lifecycle 절단 없이(연구용). 기본은 lastTradeDate 이후 NaN")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--threads", type=int, default=8)
    a = ap.parse_args(argv)
    if a.cmd == "compare":
        if not (a.a and a.b):
            ap.error("--a and --b required")
        print(json.dumps(CacheV21.compare_wide(a.a, a.b, rtol=a.rtol), indent=1, default=str))
        return 0
    if not a.root:
        ap.error("--root required")
    cv = CacheV21(a.root, master_root=a.master_root, universe_root=a.universe_root, start=a.start, end=a.end,
                  threads=a.threads)
    if a.cmd == "info":
        _cli_info(cv)
    elif a.cmd == "export":
        if not a.out:
            ap.error("--out required")
        m = cv.export_wide(a.out, fields=[f for f in a.fields.split(",") if f], basis=a.basis,
                           masks=[m for m in a.masks.split(",") if m], universes=[u for u in a.universes.split(",") if u],
                           release=True, groups=a.groups, groups_mode=a.groups_mode, cut=None if a.no_cut else "lifecycle")
        print(json.dumps({k: v for k, v in m.items() if k != "ids"}, indent=1))
    else:
        _cli_bench(cv, Path(a.out) if a.out else None, cut=None if a.no_cut else "lifecycle")
    return 0


if __name__ == "__main__":
    sys.exit(main())
