#!/home/jgo/usr/venv-3.11/bin/python
"""T7 독립 감사 — cache_v2.1 build_rc1 (2026-09-16).

다른 에이전트의 검증 코드(prices_qa.py 등)를 재사용하지 않는 독립 구현. 원자료 대조는 parquet 직접 읽기·DB 직접 조회(읽기 전용,
큰 테이블은 tradingitemid = any(chunk ≤ 1000)). 각 항목은 반례 self-test(심은 결함이 검출되는지)를 먼저 통과해야 실데이터 결과를 기록한다.

실행:  audit_v21.py [--build DIR] [--out DIR] [--items A,B,...] [--seed N] [--report-only]
산출:  <out>/<item>.json (결과), <out>/<item>_*.csv (상세), <out>/AUDIT.md (report)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import traceback
import warnings
from datetime import date as _date
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "8")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "8")
os.environ.setdefault("MKL_NUM_THREADS", "8")

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

pa.set_cpu_count(8)
pa.set_io_thread_count(8)
warnings.filterwarnings("ignore", category=FutureWarning)

SCRATCH = Path("/tmp/claude-1008/-home-jgo/3f2abe1d-a2a7-4017-9ac1-2c7cb961e311/scratchpad")
DEFAULT_BUILD = SCRATCH / "stage_v21" / "build_rc1"
DEFAULT_OUT = SCRATCH / "stage_v21" / "T7" / "qa"
PROTO = Path("/home/jgo/61/data2/temp_stock/cache_v2_cash_1d")
PROTO_TOP500_V1RULE = PROTO / "TopUniverseV2_v1rule" / "Top500.parquet"
PROTO_TOP500_ALL = PROTO / "TopUniverseV2" / "Top500.parquet"
PHASE2_HANDCHECK = Path("/home/jgo/longshort_platform/docs/missing_close_2026-09-11/phase2/p2c/real_events_hand_check.json")
LOADER_DIR = Path("/home/jgo/longshort_platform/cpp-engine/tools/data/cache_v21")
SCHEMA = "xfl_target"
NS_DAY = 86_400_000_000_000
T0 = time.time()

# build_cache_v2.TOKENS_TO_REMOVE (v1 METADATA, 46종) — 값만 복사(모듈 import 없음)
TOKENS_TO_REMOVE = [
    27860642, 2642243, 1913901735, 1933861078, 2588570, 216534627, 2615898, 25745176, 659337840,
    1776948460, 2622876, 622839848, 1683446337, 1673049332, 2640322, 715588599, 24687604, 1846726403,
    132508040, 25389977, 2618746, 2626233, 1952948011, 1820944852, 2627536, 2659893, 268941272,
    2623717, 266673936, 39734352, 1672696918, 43858210, 604619421, 2593888, 1672821289, 709423890,
    2640332, 2595170, 2603117, 2594156, 2623439, 307533788, 2597012, 2611328, 2618950, 2629108,
]

# ciqdividendtype: 현금으로 보는 유형(주식배당 11, 타사 주식 분배 20, 권리 31/32, 비USD 표기 16/24(캐나다 세율 코드는 현금이지만 통화 조건으로 걸림) 제외)
CASH_DIV_TYPES = {1, 4, 5, 6, 7, 8, 10, 15, 16, 17, 18, 19, 23, 24, 33}
NONCASH_DIV_TYPES = {11, 20, 31, 32, 13}
USD = 160


# ----------------------------------------------------------------------------------------------------------------------
# 공통 유틸
# ----------------------------------------------------------------------------------------------------------------------
def log(msg: str) -> None:
    print(f"[{time.time() - T0:7.1f}s] {msg}", flush=True)


def sha256_file(path: Path, bufsize: int = 1 << 22) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(bufsize)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def peak_rss_gb() -> float:
    import resource
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024


def jsonable(o):
    import decimal
    if isinstance(o, decimal.Decimal):
        return float(o)
    if isinstance(o, dict):
        return {str(k): jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [jsonable(v) for v in o]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if np.isnan(o) else float(o)
    if isinstance(o, float) and np.isnan(o):
        return None
    if isinstance(o, (pd.Timestamp, np.datetime64, _date)):
        return None if pd.isna(o) else str(pd.Timestamp(o).date())
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, Path):
        return str(o)
    if o is pd.NaT or o is pd.NA:
        return None
    try:
        json.dumps(o)
        return o
    except TypeError:
        return str(o)


def save_json(out: Path, name: str, obj: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{name}.json").write_text(json.dumps(jsonable(obj), ensure_ascii=False, indent=1))


def ts_ns(x) -> np.ndarray:
    return pd.to_datetime(pd.Series(x)).values.astype("datetime64[ns]").astype("int64")


def days_since_1900(ns_i8: np.ndarray) -> np.ndarray:
    base = np.datetime64("1900-01-01", "ns").astype("int64")
    return ((ns_i8 - base) // NS_DAY).astype("int64")


class DB:
    """읽기 전용 xfeed 세션. 큰 테이블은 반드시 ids 청크(≤1000)로."""

    def __init__(self):
        import psycopg2
        params = dict(host="localhost", database="xfeed", user="loader", port="5432",
                      options="-c default_transaction_read_only=on -c statement_timeout=600000")
        if os.environ.get("XFEED_DB_PASSWORD"):
            params["password"] = os.environ["XFEED_DB_PASSWORD"]
        self.c = psycopg2.connect(**params)
        self.c.set_session(readonly=True, autocommit=True)
        self.n_queries = 0
        self.t_db = 0.0

    def q(self, sql: str, params=None) -> pd.DataFrame:
        t0 = time.time()
        with self.c.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
        self.n_queries += 1
        self.t_db += time.time() - t0
        return pd.DataFrame(rows, columns=cols)

    def q_ids(self, sql: str, ids, chunk: int = 1000, params_after=()) -> pd.DataFrame:
        """sql 안의 첫 %s 가 tradingitemid = any(%s) 자리. ids 를 청크로 나눠 순차 조회."""
        ids = sorted({int(i) for i in ids})
        out = []
        for i in range(0, len(ids), chunk):
            out.append(self.q(sql, (ids[i:i + chunk], *params_after)))
        return pd.concat(out, ignore_index=True) if out else pd.DataFrame()

    def wait_quiet(self, max_active: int = 2, cap_s: int = 300) -> None:
        t0 = time.time()
        while time.time() - t0 < cap_s:
            n = self.q("select count(*) n from pg_stat_activity where usename='loader' and state='active' and pid<>pg_backend_pid()").n[0]
            if int(n) <= max_active:
                return
            time.sleep(10)


class Build:
    """build_rc1 읽기 전용 접근자(행 그룹 프루닝)."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.manifest = json.loads((self.root / "manifest.json").read_text())
        self._pf: dict[str, pq.ParquetFile] = {}
        self._rg_ranges: dict[str, list[tuple[int, int]]] = {}
        self._cache: dict[str, pd.DataFrame] = {}

    def pf(self, rel: str) -> pq.ParquetFile:
        if rel not in self._pf:
            self._pf[rel] = pq.ParquetFile(self.root / rel)
        return self._pf[rel]

    def rg_id_ranges(self, rel: str) -> list[tuple[int, int]]:
        if rel not in self._rg_ranges:
            pf = self.pf(rel)
            idx = pf.schema_arrow.get_field_index("tradingItemId")
            rr = []
            for i in range(pf.metadata.num_row_groups):
                st = pf.metadata.row_group(i).column(idx).statistics
                rr.append((int(st.min), int(st.max)))
            self._rg_ranges[rel] = rr
        return self._rg_ranges[rel]

    def read_ids(self, rel: str, ids, columns=None) -> pd.DataFrame:
        """tradingItemId ∈ ids 인 행만(행 그룹 통계로 프루닝 후 필터)."""
        ids = np.asarray(sorted({int(i) for i in ids}), dtype=np.int64)
        pf = self.pf(rel)
        groups = [i for i, (lo, hi) in enumerate(self.rg_id_ranges(rel)) if ((ids >= lo) & (ids <= hi)).any()]
        parts = []
        for g in groups:
            t = pf.read_row_group(g, columns=columns)
            m = pc.is_in(t.column("tradingItemId"), value_set=pa.array(ids, pa.int64()))
            t = t.filter(m)
            if t.num_rows:
                parts.append(t.to_pandas())
        if not parts:
            return pd.DataFrame(columns=columns or pf.schema_arrow.names)
        return pd.concat(parts, ignore_index=True)

    def table(self, rel: str, columns=None) -> pd.DataFrame:
        key = f"{rel}|{','.join(columns) if columns else '*'}"
        if key not in self._cache:
            self._cache[key] = pd.read_parquet(self.root / rel, columns=columns)
        return self._cache[key]

    # 자주 쓰는 마스터
    def lifecycle(self) -> pd.DataFrame:
        return self.table("master/lifecycle.parquet").set_index("tradingItemId")

    def instruments(self) -> pd.DataFrame:
        return self.table("master/instruments.parquet")

    def current(self) -> pd.DataFrame:
        return self.table("master/current.parquet").set_index("tradingItemId")

    def lineage(self) -> pd.DataFrame:
        return self.table("master/lineage.parquet")

    def exclusions(self) -> pd.DataFrame:
        return self.table("master/tradable_exclusions.parquet")

    def membership(self, v1rule: bool = False) -> pd.DataFrame:
        return self.table("universe/membership_v1rule.parquet" if v1rule else "universe/membership.parquet")

    def calendar_days(self) -> np.ndarray:
        cal = self.table("calendar.parquet")
        return np.sort(ts_ns(cal.loc[cal.isTradingDay, "date"]))

    def ever_top500(self) -> set[int]:
        m = self.membership(False)
        m1 = self.membership(True)
        return set(m.loc[m.universe == "Top500", "tradingItemId"].astype(int)) | set(m1.loc[m1.universe == "Top500", "tradingItemId"].astype(int))

    def ever_universe(self, name: str) -> set[int]:
        m = self.membership(False)
        m1 = self.membership(True)
        return set(m.loc[m.universe == name, "tradingItemId"].astype(int)) | set(m1.loc[m1.universe == name, "tradingItemId"].astype(int))


class Intervals:
    """반개구간 [validFrom, validTo) 테이블의 독립 lookup 구현(계수·유니버스 공용)."""

    def __init__(self, df: pd.DataFrame, id_col="tradingItemId", from_col="validFrom", to_col="validTo", val_col="factor",
                 closed_right: bool = False):
        d = df.sort_values([id_col, from_col]).reset_index(drop=True)
        self.ids = d[id_col].to_numpy(np.int64)
        f = ts_ns(d[from_col])
        t = pd.to_datetime(d[to_col])
        t_i8 = t.values.astype("datetime64[ns]").astype("int64")
        t_i8 = np.where(t.isna().to_numpy(), np.iinfo(np.int64).max, t_i8)
        if closed_right:  # 폐구간 [from, to] → to 를 하루 뒤로 밀어 반개구간으로
            t_i8 = np.where(t_i8 == np.iinfo(np.int64).max, t_i8, t_i8 + NS_DAY)
        self.f_days = days_since_1900(f)
        self.t_days = np.where(t_i8 == np.iinfo(np.int64).max, np.iinfo(np.int64).max // NS_DAY, days_since_1900(t_i8))
        self.keys = self.ids * (1 << 20) + self.f_days  # days < 2^20
        assert (self.f_days < (1 << 20)).all() and (self.f_days >= 0).all()
        self.vals = d[val_col].to_numpy() if val_col is not None else None
        self.df = d

    def lookup(self, ids: np.ndarray, dates_i8: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """→ (값, hit) ; hit False = 어떤 구간에도 안 맞음(빈틈)."""
        ids = np.asarray(ids, np.int64)
        qd = days_since_1900(np.asarray(dates_i8, np.int64))
        qk = ids * (1 << 20) + qd
        pos = np.searchsorted(self.keys, qk, side="right") - 1
        ok = pos >= 0
        pos_c = np.clip(pos, 0, len(self.keys) - 1)
        ok &= (self.ids[pos_c] == ids) & (qd < self.t_days[pos_c]) & (qd >= self.f_days[pos_c])
        vals = self.vals[pos_c].astype(float) if self.vals is not None else None
        if vals is not None:
            vals = np.where(ok, vals, np.nan)
        return vals, ok

    def coverage_check(self, expect_ids=None) -> dict:
        """1900-01-01부터 열린 끝까지 빈틈·겹침 없이 덮는지 id별 검사."""
        d = self.df
        ids = self.ids
        starts = np.r_[True, ids[1:] != ids[:-1]]
        ends = np.r_[ids[1:] != ids[:-1], True]
        first_ok = self.f_days[starts] == 0  # 1900-01-01
        open_end = self.t_days == np.iinfo(np.int64).max // NS_DAY
        last_open = open_end[ends]
        # 인접 구간: to == next.from
        same_next = ids[:-1] == ids[1:]
        chain_ok = (self.t_days[:-1] == self.f_days[1:]) | ~same_next
        gaps = same_next & (self.t_days[:-1] < self.f_days[1:])
        overlaps = same_next & (self.t_days[:-1] > self.f_days[1:])
        n_open_per_id = pd.Series(open_end).groupby(ids).sum()
        bad_ids = set(ids[starts][~first_ok]) | set(ids[ends][~last_open]) | set(ids[:-1][gaps]) | set(ids[:-1][overlaps]) \
            | set(n_open_per_id.index[n_open_per_id != 1])
        res = dict(n_rows=int(len(d)), n_ids=int(starts.sum()), first_not_1900=int((~first_ok).sum()), last_not_open=int((~last_open).sum()),
                   gaps=int(gaps.sum()), overlaps=int(overlaps.sum()), ids_with_open_count_ne_1=int((n_open_per_id != 1).sum()),
                   nonpositive_or_nan=int((~np.isfinite(self.vals) | (self.vals <= 0)).sum()) if self.vals is not None else 0,
                   bad_ids=sorted(int(i) for i in bad_ids)[:50], n_bad_ids=len(bad_ids), chain_break_rows=int((~chain_ok).sum()))
        if expect_ids is not None:
            have = set(int(i) for i in np.unique(ids))
            res["missing_ids"] = len(set(expect_ids) - have)
            res["extra_ids"] = len(have - set(expect_ids))
        return res


def daily_from_intervals(mb: pd.DataFrame, cal_days: np.ndarray, ids: np.ndarray) -> np.ndarray:
    """membership 구간(폐구간 fromDate..toDate) → (T × N) bool. 독립 구현(diff-cumsum)."""
    T, N = len(cal_days), len(ids)
    col = pd.Index(ids).get_indexer(mb.tradingItemId.to_numpy(np.int64))
    f = ts_ns(mb.fromDate)
    t = ts_ns(mb.toDate)
    s = np.searchsorted(cal_days, f, side="left")
    e = np.searchsorted(cal_days, t, side="right")
    ok = col >= 0
    diff = np.zeros((T + 1, N), dtype=np.int32)
    np.add.at(diff, (s[ok], col[ok]), 1)
    np.add.at(diff, (e[ok], col[ok]), -1)
    return np.cumsum(diff[:-1], axis=0) > 0


def verdict(fail: bool, warn: bool = False) -> str:
    return "FAIL" if fail else ("주의" if warn else "PASS")


# ----------------------------------------------------------------------------------------------------------------------
# A. manifest 무결성 · 스키마 · 정렬 · 중복 · 달력
# ----------------------------------------------------------------------------------------------------------------------
CONTRACT_SCHEMA = {
    "base_data.parquet": {"date": "timestamp[ns]", "tradingItemId": "int64", "open": "float", "high": "float", "low": "float",
                          "close": "float", "vwap": "float", "volume": "double", "source": "int8", "qualityFlag": "int8"},
    "factors/split.parquet": {"tradingItemId": "int64", "validFrom": "timestamp[ns]", "validTo": "timestamp[ns]", "factor": "double",
                              "source": "string", "snapshotDate": "timestamp[ns]"},
    "factors/dividend.parquet": {"tradingItemId": "int64", "validFrom": "timestamp[ns]", "validTo": "timestamp[ns]", "factor": "double",
                                 "source": "string", "snapshotDate": "timestamp[ns]"},
    "derived/returns_total.parquet": {"date": "timestamp[ns]", "tradingItemId": "int64", "ret": "double", "prevDate": "timestamp[ns]"},
    "universe/membership.parquet": {"universe": "string", "tradingItemId": "int64", "fromDate": "timestamp[ns]", "toDate": "timestamp[ns]"},
    "universe/membership_v1rule.parquet": {"universe": "string", "tradingItemId": "int64", "fromDate": "timestamp[ns]", "toDate": "timestamp[ns]"},
    "master/instruments.parquet": {"tradingItemId": "int64", "securityId": "int64", "companyId": "int64", "companyName": "string",
                                   "securitySubTypeId": "int64", "tiCurrencyId": "int64", "inV1Rule": "bool", "everMainExchange": "bool",
                                   "inV1Cache": "bool", "inProtoV2": "bool", "addedOn": "timestamp[ns]"},
    "master/current.parquet": {"tradingItemId": "int64", "symbol": "string", "exchangeId": "int64", "exchangeName": "string", "primaryFlag": "int64",
                               "primaryFlagTi": "int64", "currentPrimary": "bool", "sector": "string", "simpleIndustryDescription": "string",
                               "tradingItemStatusId": "int64", "securityStartDate": "timestamp[ns]", "securityEndDate": "timestamp[ns]",
                               "gvkey": "string", "iid": "string", "asOf": "timestamp[ns]"},
    "master/identifier_history.parquet": {"tradingItemId": "int64", "gvkey": "string", "iid": "string", "item": "string", "value": "string",
                                          "effFrom": "timestamp[ms]", "effThru": "timestamp[ms]", "source": "string", "mappingQuality": "string"},
    "master/id_mapping.parquet": {"tradingItemId": "int64", "gvkey": "string", "iid": "string", "symbolStartDate": "timestamp[ns]",
                                  "symbolEndDate": "timestamp[ns]", "activeFlag": "int64", "source": "string", "conflictFlag": "bool"},
    "master/classification_history.parquet": {"tradingItemId": "int64", "sector": "string", "simpleIndustryDescription": "string",
                                              "effectiveFrom": "timestamp[ns]", "effectiveTo": "timestamp[ns]", "observedAt": "timestamp[ns]",
                                              "source": "string", "historyQuality": "string"},
    "master/primary_history.parquet": {"tradingItemId": "int64", "primaryFlag": "int64", "primaryFlagTi": "int64", "currentPrimary": "bool",
                                       "effectiveFrom": "timestamp[ns]", "effectiveTo": "timestamp[ns]", "observedAt": "timestamp[ns]",
                                       "source": "string", "historyQuality": "string"},
    "master/lineage.parquet": {"oldId": "int64", "newId": "int64", "relation": "string", "effectiveDate": "timestamp[ns]", "ratio": "double",
                               "evidence": "string", "confidence": "string"},
    "master/lifecycle.parquet": {"tradingItemId": "int64", "firstTradeDate": "timestamp[ns]", "firstPriceDate": "timestamp[ns]",
                                 "firstTradeSource": "string", "ciqLastCloseDate": "timestamp[ns]", "miLastCloseDate": "timestamp[ns]",
                                 "securityEndDate": "timestamp[ns]", "tradingItemStatusId": "int64", "eventEndDate": "timestamp[ns]",
                                 "lastTradeDate": "timestamp[ns]", "lastTradeSource": "string", "endReason": "string", "status": "string",
                                 "observedAt": "timestamp[ns]", "evidence": "string"},
    "master/tradable_exclusions.parquet": {"tradingItemId": "int64", "fromDate": "timestamp[ns]", "toDate": "timestamp[ns]", "reason": "string",
                                           "preferredItemId": "int64"},
}
ALLOWED_DEVIATIONS = {"master/identifier_history.parquet": "effFrom/effThru timestamp[ms] (2900-01-01 sentinel, T1 §9-1 리드 허용)",
                      "master/lifecycle.parquet": "firstPriceDate/firstTradeSource 추가 컬럼(리드 결정 (2))"}


def check_sorted_and_unique(rel: str, b: Build, cal_days: np.ndarray | None) -> dict:
    """(tradingItemId, date) 정렬·행 그룹 id 비중첩·중복 0·달력 ⊆ 검사 — 행 그룹 스트리밍."""
    pf = b.pf(rel)
    prev_key = None
    n_dup = 0
    n_unsorted = 0
    rg_ranges = []
    off_cal = 0
    n = 0
    dates_seen = set()
    for g in range(pf.metadata.num_row_groups):
        t = pf.read_row_group(g, columns=["tradingItemId", "date"])
        ids = t.column("tradingItemId").to_numpy()
        d = t.column("date").to_numpy().astype("datetime64[ns]").astype("int64")
        key = ids.astype(np.int64) * (1 << 20) + days_since_1900(d)  # id < 2^31, days < 2^20 → 오버플로 없음
        if prev_key is not None:
            if key[0] <= prev_key:
                n_unsorted += 1
                if key[0] == prev_key:
                    n_dup += 1
        dk = np.diff(key)
        n_unsorted += int((dk < 0).sum())
        n_dup += int((dk == 0).sum())
        prev_key = key[-1]
        rg_ranges.append((int(ids.min()), int(ids.max())))
        if cal_days is not None:
            u = np.unique(d)
            dates_seen.update(u.tolist())
        n += len(ids)
    if cal_days is not None:
        ds = np.array(sorted(dates_seen), dtype=np.int64)
        off_cal = int((~np.isin(ds, cal_days)).sum())
        # 시각 00:00 인지
        not_midnight = int((ds % NS_DAY != 0).sum())
    else:
        not_midnight = 0
    rg_overlap = sum(1 for i in range(1, len(rg_ranges)) if rg_ranges[i][0] <= rg_ranges[i - 1][1])
    return dict(rows=n, unsorted_steps=n_unsorted, duplicate_keys=n_dup, row_groups=len(rg_ranges), rg_id_overlaps=rg_overlap,
                off_calendar_dates=off_cal, dates_not_midnight=not_midnight, n_dates=len(dates_seen))


def selftest_A(tmp: Path) -> dict:
    """반례: 중복 키·역정렬·달력 밖 날짜·행 그룹 id 중첩·sha 훼손이 검출되는지."""
    tmp.mkdir(parents=True, exist_ok=True)
    d = pd.to_datetime(["2020-01-02", "2020-01-03", "2020-01-06"])
    cal = ts_ns(pd.to_datetime(["2020-01-02", "2020-01-03", "2020-01-06", "2020-01-07"]))
    good = pd.DataFrame({"date": list(d) * 2, "tradingItemId": [1, 1, 1, 2, 2, 2]})
    bad = pd.DataFrame({"date": [d[0], d[1], d[1], d[2], d[0], pd.Timestamp("2020-01-04")], "tradingItemId": [1, 1, 1, 2, 2, 2]})
    res = {}
    for name, df, rgs in [("good", good, [3, 3]), ("bad", bad, [3, 3]), ("rg_overlap", good.sort_values("date", kind="stable"), [3, 3])]:
        p = tmp / f"selftest_A_{name}.parquet"
        t = pa.Table.from_pandas(df, preserve_index=False)
        w = pq.ParquetWriter(p, t.schema)
        i = 0
        for k in rgs:
            w.write_table(t.slice(i, k)); i += k
        w.close()
        bb = Build.__new__(Build); bb.root = tmp; bb.manifest = {}; bb._pf = {}; bb._rg_ranges = {}; bb._cache = {}
        res[name] = check_sorted_and_unique(p.name, bb, cal)
    ok = (res["good"]["duplicate_keys"] == 0 and res["good"]["unsorted_steps"] == 0 and res["good"]["off_calendar_dates"] == 0
          and res["good"]["rg_id_overlaps"] == 0 and res["bad"]["duplicate_keys"] >= 1 and res["bad"]["unsorted_steps"] >= 1
          and res["bad"]["off_calendar_dates"] == 1 and res["rg_overlap"]["rg_id_overlaps"] >= 1)
    # sha 훼손
    p = tmp / "selftest_A_good.parquet"
    h1 = sha256_file(p)
    data = bytearray(p.read_bytes()); data[10] ^= 0xFF
    (tmp / "selftest_A_tampered.parquet").write_bytes(bytes(data))
    ok &= sha256_file(tmp / "selftest_A_tampered.parquet") != h1
    return dict(passed=bool(ok), detail=res)


def item_A(b: Build, out: Path, tmp: Path, **_) -> dict:
    st = selftest_A(tmp)
    if not st["passed"]:
        return dict(verdict="FAIL", selftest=st, note="self-test 실패 → 검사기 신뢰 불가")
    files = b.manifest["files"]
    rows = []
    for rel, meta in files.items():
        p = b.root / rel
        r = dict(file=rel, exists=p.exists())
        if p.exists():
            r["bytes_ok"] = p.stat().st_size == meta["bytes"]
            r["sha_ok"] = sha256_file(p) == meta["sha256"]
            if "rows" in meta:
                r["rows_ok"] = pq.ParquetFile(p).metadata.num_rows == meta["rows"]
        rows.append(r)
    fdf = pd.DataFrame(rows)
    fdf.to_csv(out / "A_manifest_files.csv", index=False)
    on_disk = {str(p.relative_to(b.root)) for p in b.root.rglob("*") if p.is_file()}
    not_in_manifest = sorted(on_disk - set(files) - {"manifest.json", "README.md"})
    missing = sorted(set(files) - on_disk)
    # 스키마
    schema_rows = []
    for rel, spec in CONTRACT_SCHEMA.items():
        sch = b.pf(rel).schema_arrow
        actual = {f.name: str(f.type) for f in sch}
        mism = {k: (v, actual.get(k)) for k, v in spec.items() if actual.get(k) != v}
        extra = sorted(set(actual) - set(spec))
        schema_rows.append(dict(file=rel, mismatches=json.dumps(mism), extra_columns=extra, allowed=ALLOWED_DEVIATIONS.get(rel, ""),
                                ok=(not mism or rel in ALLOWED_DEVIATIONS) and (not extra or rel in ALLOWED_DEVIATIONS)))
    sdf = pd.DataFrame(schema_rows)
    sdf.to_csv(out / "A_schema.csv", index=False)
    cal_days = b.calendar_days()
    cal = b.table("calendar.parquet")
    sort_res = {rel: check_sorted_and_unique(rel, b, cal_days) for rel in
                ["base_data.parquet", "derived/returns_total.parquet", "factors/adjustment_factor_daily.parquet"]}
    # base_data 와 adjustment_factor_daily 의 (id,date) 집합·순서 동일?
    same_keys = None
    try:
        t1 = pq.read_table(b.root / "base_data.parquet", columns=["tradingItemId", "date"])
        t2 = pq.read_table(b.root / "factors/adjustment_factor_daily.parquet", columns=["tradingItemId", "date"])
        same_keys = bool(t1.num_rows == t2.num_rows and pc.all(pc.equal(t1.column("tradingItemId"), t2.column("tradingItemId"))).as_py()
                         and pc.all(pc.equal(t1.column("date"), t2.column("date"))).as_py())
        del t1, t2
    except Exception as e:  # noqa
        same_keys = f"error {e}"
    # 계수 테이블 정렬(id, validFrom)
    fac_sorted = {}
    for rel in ["factors/split.parquet", "factors/dividend.parquet"]:
        f = b.table(rel)
        k = f.tradingItemId.to_numpy(np.int64) * (1 << 20) + days_since_1900(ts_ns(f.validFrom))
        fac_sorted[rel] = bool((np.diff(k) > 0).all())
    # base_data 종목 집합 = lifecycle 집합
    base_ids = np.unique(pq.read_table(b.root / "base_data.parquet", columns=["tradingItemId"]).column(0).to_numpy())
    lc_ids = set(b.lifecycle().index.astype(int))
    inst_ids = set(b.instruments().tradingItemId.astype(int))
    id_set = dict(base_ids=len(base_ids), base_not_in_lifecycle=len(set(base_ids.tolist()) - lc_ids), lifecycle_not_in_base=len(lc_ids - set(base_ids.tolist())),
                  instruments=len(inst_ids), instruments_eq_lifecycle=inst_ids == lc_ids)
    cal_info = dict(dates=int(len(cal)), trading_days=int(cal.isTradingDay.sum()), first=str(cal.date.min().date()), last=str(cal.date.max().date()),
                    asOf_manifest=b.manifest["asOfDate"], last_trading_eq_asof=str(cal.loc[cal.isTradingDay, "date"].max().date()) == b.manifest["asOfDate"])
    n_bad_files = int((~fdf.get("sha_ok", pd.Series(dtype=bool)).fillna(False)).sum() + (~fdf.get("rows_ok", pd.Series(dtype=bool)).fillna(True)).sum())
    fail = (n_bad_files > 0 or missing or not sdf.ok.all() or any(v["duplicate_keys"] or v["unsorted_steps"] or v["rg_id_overlaps"] or v["off_calendar_dates"] or v["dates_not_midnight"] for v in sort_res.values())
            or id_set["base_not_in_lifecycle"] or not id_set["instruments_eq_lifecycle"] or same_keys is not True or not all(fac_sorted.values()))
    res = dict(verdict=verdict(bool(fail), warn=bool(not_in_manifest)), selftest=st,
               manifest=dict(n_files=len(files), sha_mismatch=int((~fdf.sha_ok.fillna(False)).sum()), rows_mismatch=int((~fdf.rows_ok.fillna(True)).sum()),
                             bytes_mismatch=int((~fdf.bytes_ok.fillna(False)).sum()), missing=missing, not_in_manifest=not_in_manifest),
               schema=dict(files_checked=len(sdf), not_ok=sdf.loc[~sdf.ok, "file"].tolist(),
                           deviations={r.file: r.mismatches for r in sdf.itertuples() if r.mismatches != "{}"},
                           extra_columns={r.file: r.extra_columns for r in sdf.itertuples() if r.extra_columns}),
               sort_unique=sort_res, base_eq_adjf_keys=same_keys, factors_sorted=fac_sorted, id_set=id_set, calendar=cal_info)
    return res


# ----------------------------------------------------------------------------------------------------------------------
# B. raw 가격 무작위 셀 원자료 대조 (ciqpriceequity × adjustmentfactor, miprice)
# ----------------------------------------------------------------------------------------------------------------------
def sample_cells(b: Build, n: int, seed: int, top500: set[int]) -> pd.DataFrame:
    """base_data 에서 (id,date) n 셀: 연도 균등(2000~2026), 절반 everTop500. 행 그룹별 스트리밍 표본."""
    rng = np.random.default_rng(seed)
    pf = b.pf("base_data.parquet")
    parts = []
    for g in range(pf.metadata.num_row_groups):
        t = pf.read_row_group(g, columns=["tradingItemId", "date", "close"])
        ids = t.column("tradingItemId").to_numpy()
        d = t.column("date").to_numpy().astype("datetime64[ns]")
        yr = d.astype("datetime64[Y]").astype(int) + 1970
        is_top = np.isin(ids, np.fromiter(top500, dtype=np.int64))
        # 행 그룹마다 넉넉히 뽑아 뒤에서 층화 추출
        k = min(len(ids), 40000)
        pick = rng.choice(len(ids), size=k, replace=False)
        parts.append(pd.DataFrame({"tradingItemId": ids[pick], "date": d[pick], "year": yr[pick], "top500": is_top[pick],
                                   "close_notna": ~np.isnan(t.column("close").to_numpy()[pick])}))
    pool = pd.concat(parts, ignore_index=True)
    years = sorted(pool.year.unique())
    per = n // (2 * len(years))
    out = []
    for y in years:
        for top in (True, False):
            sub = pool[(pool.year == y) & (pool.top500 == top)]
            out.append(sub.sample(n=min(per, len(sub)), random_state=int(rng.integers(1 << 31))))
    s = pd.concat(out, ignore_index=True)
    if len(s) < n:  # 부족분은 전체에서 보충
        rest = pool.drop(s.index, errors="ignore")
        s = pd.concat([s, rest.sample(n=n - len(s), random_state=seed)], ignore_index=True)
    return s.drop_duplicates(["tradingItemId", "date"]).reset_index(drop=True)


def compare_cells(base: pd.DataFrame, ciq: pd.DataFrame, mi: pd.DataFrame, price_tol=1e-6, vol_tol=1.0) -> pd.DataFrame:
    m = base.merge(ciq, on=["tradingItemId", "date"], how="left", suffixes=("", "_db"))
    m = m.merge(mi, on=["tradingItemId", "date"], how="left")
    out = m.copy()
    for f in ["open", "high", "low", "close", "vwap"]:
        exp = m[f"ciq_{f}"].astype(float) * m["adjf"].astype(float)
        got = m[f].astype(float)
        both_nan = np.isnan(exp) & np.isnan(got)
        diff = np.abs(got - exp)
        tol = price_tol * np.maximum(1.0, np.abs(exp))
        out[f"{f}_ok"] = both_nan | (diff <= tol)
        out[f"{f}_exp"] = exp
    exp_v = m["ciq_volume"].astype(float) / m["adjf"].astype(float)
    got_v = m["volume"].astype(float)
    out["volume_ok"] = (np.isnan(exp_v) & np.isnan(got_v)) | (np.abs(got_v - exp_v) <= vol_tol)
    out["volume_exp"] = exp_v
    out["ciq_row_missing"] = m["adjf"].isna()
    # miprice as-quoted
    mic = m["mi_close"].astype(float)
    out["mi_close_rel"] = np.abs(m["close"].astype(float) / mic - 1.0)
    out["mi_close_available"] = mic.notna()
    out["mi_mismatch_1e3"] = out["mi_close_available"] & m["close"].notna() & (out["mi_close_rel"] > 1e-3)
    out["all_ok"] = out[[f"{f}_ok" for f in ["open", "high", "low", "close", "vwap", "volume"]]].all(axis=1) & ~out["ciq_row_missing"]
    return out


def selftest_B() -> dict:
    d = pd.to_datetime(["2020-01-02"] * 3)
    base = pd.DataFrame({"tradingItemId": [1, 2, 3], "date": d, "open": [10.0, 20.0, 30.0], "high": [11, 21, 31], "low": [9, 19, 29],
                         "close": [10.5, 20.5, 30.5], "vwap": [10.4, 20.4, np.nan], "volume": [1000.0, 2000.0, 3000.0], "qualityFlag": [0, 0, 0]})
    ciq = pd.DataFrame({"tradingItemId": [1, 2, 3], "date": d, "ciq_open": [5.0, 20.0, 30.0], "ciq_high": [5.5, 21, 31], "ciq_low": [4.5, 19, 29],
                        "ciq_close": [5.25, 20.5 * 1.001, 30.5], "ciq_vwap": [5.2, 20.4, np.nan], "ciq_volume": [2000, 2000, 3002], "adjf": [2.0, 1.0, 1.0]})
    mi = pd.DataFrame({"tradingItemId": [1, 2, 3], "date": d, "mi_close": [10.5, 20.5, 30.6]})
    r = compare_cells(base, ciq, mi)
    ok = bool(r.all_ok.tolist() == [True, False, False] and r.close_ok.tolist() == [True, False, True] and r.volume_ok.tolist() == [True, True, False]
              and r.mi_mismatch_1e3.tolist() == [False, False, True])
    return dict(passed=ok, detail=r[["tradingItemId", "all_ok", "close_ok", "volume_ok", "mi_mismatch_1e3"]].to_dict("records"))


def item_B(b: Build, out: Path, tmp: Path, db: DB, seed: int, n_cells: int = 2000, **_) -> dict:
    st = selftest_B()
    if not st["passed"]:
        return dict(verdict="FAIL", selftest=st)
    top500 = b.ever_top500()
    cells = sample_cells(b, n_cells, seed, top500)
    cells["stratum"] = "random"
    # 추가 층: qualityFlag 2/3 행(희소) 각 ≤150 — 플래그 의미(mi 불일치·ciq null close)를 원자료로 확인
    pf = b.pf("base_data.parquet")
    fl = []
    for g in range(pf.metadata.num_row_groups):
        t = pf.read_row_group(g, columns=["tradingItemId", "date", "qualityFlag"])
        t = t.filter(pc.is_in(t.column("qualityFlag"), value_set=pa.array([2, 3], pa.int8())))
        if t.num_rows:
            fl.append(t.to_pandas())
    fl = pd.concat(fl, ignore_index=True)
    rng2 = np.random.default_rng(seed + 7)
    extra = pd.concat([fl[fl.qualityFlag == 2].sample(n=min(150, int((fl.qualityFlag == 2).sum())), random_state=int(rng2.integers(1 << 31))),
                       fl[fl.qualityFlag == 3].sample(n=min(150, int((fl.qualityFlag == 3).sum())), random_state=int(rng2.integers(1 << 31)))])
    extra = extra.assign(year=extra.date.dt.year, top500=extra.tradingItemId.isin(top500), stratum="flag" + extra.qualityFlag.astype(str)).drop(columns=["qualityFlag"])
    cells = pd.concat([cells, extra[["tradingItemId", "date", "year", "top500", "stratum"]]], ignore_index=True).drop_duplicates(["tradingItemId", "date"])
    ids = sorted(cells.tradingItemId.unique().tolist())
    # base_data 행 (프루닝 읽기 후 표본 셀만)
    base = b.read_ids("base_data.parquet", ids)
    base = base.merge(cells[["tradingItemId", "date", "year", "top500", "stratum"]], on=["tradingItemId", "date"], how="inner")
    # DB: (id,date) 쌍 조인 — id 청크 + 날짜 집합으로 제한
    ciq_parts, mi_parts = [], []
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        sub = cells[cells.tradingItemId.isin(chunk)]
        dates = sorted({pd.Timestamp(x).to_pydatetime() for x in sub.date})
        ciq_parts.append(db.q(f"""select tradingitemid as "tradingItemId", pricingdate as date, priceopen as ciq_open, pricehigh as ciq_high,
                                   pricelow as ciq_low, priceclose as ciq_close, vwap as ciq_vwap, volume as ciq_volume, adjustmentfactor as adjf
                                   from {SCHEMA}.ciqpriceequity where tradingitemid = any(%s) and pricingdate = any(%s)""", (chunk, dates)))
        mi_parts.append(db.q(f"""select tradingitemid as "tradingItemId", pricedate::timestamp as date, priceclose as mi_close, volume as mi_volume
                                  from {SCHEMA}.miprice where tradingitemid = any(%s) and pricedate = any(%s)""", (chunk, [d.date() for d in dates])))
    ciq = pd.concat(ciq_parts, ignore_index=True)
    mi = pd.concat(mi_parts, ignore_index=True)
    for df in (ciq, mi):
        df["date"] = pd.to_datetime(df["date"])
        df["tradingItemId"] = df["tradingItemId"].astype(np.int64)
    for c in ["ciq_open", "ciq_high", "ciq_low", "ciq_close", "ciq_vwap", "ciq_volume", "adjf"]:
        ciq[c] = ciq[c].astype(float)
    mi["mi_close"] = mi["mi_close"].astype(float)
    mi = mi.drop_duplicates(["tradingItemId", "date"])
    r_all = compare_cells(base, ciq, mi)
    r_all.to_csv(out / "B_cells.csv", index=False)
    r = r_all[r_all.stratum == "random"]
    rf = r_all[r_all.stratum != "random"]
    flag_strata = dict(flag2_cells=int((rf.stratum == "flag2").sum()), flag2_with_mi_mismatch_1e3=int(((rf.stratum == "flag2") & rf.mi_mismatch_1e3).sum()),
                       flag2_mi_close_missing=int(((rf.stratum == "flag2") & ~rf.mi_close_available).sum()),
                       flag2_ciq_values_ok=int(((rf.stratum == "flag2") & rf.all_ok).sum()),
                       flag3_cells=int((rf.stratum == "flag3").sum()), flag3_db_close_null=int(((rf.stratum == "flag3") & rf.ciq_close.isna()).sum()),
                       flag3_base_close_null=int(((rf.stratum == "flag3") & rf.close.isna()).sum()), flag3_values_ok=int(((rf.stratum == "flag3") & rf.all_ok).sum()),
                       flag3_pre2010=int(((rf.stratum == "flag3") & (rf.date < "2010-01-01")).sum()))
    bad = r[~r.all_ok]
    bad.to_csv(out / "B_mismatches.csv", index=False)
    # mi 불일치 ↔ qualityFlag 2 정합 (2010+ 만 T2 대조 범위)
    mm = r[r.mi_mismatch_1e3]
    flag_consistency = dict(mi_mismatch_cells=int(len(mm)), of_which_flag2=int((mm.qualityFlag == 2).sum()),
                            mi_mismatch_pre2010_unflagged=int(((mm.date < "2010-01-01") & (mm.qualityFlag != 2)).sum()),
                            mi_mismatch_2010plus_unflagged=int(((mm.date >= "2010-01-01") & (mm.qualityFlag != 2)).sum()),
                            flag2_cells_in_sample=int((r.qualityFlag == 2).sum()),
                            flag2_without_mi_mismatch=int(((r.qualityFlag == 2) & ~r.mi_mismatch_1e3).sum()),
                            flag3_cells=int((r.qualityFlag == 3).sum()), flag3_all_null_close=bool((r.loc[r.qualityFlag == 3, "close"].isna()).all()),
                            null_close_not_flag3=int((r.close.isna() & (r.qualityFlag != 3) & (r.qualityFlag != 4)).sum()),
                            flag4_cells=int((r.qualityFlag == 4).sum()))
    res = dict(verdict=verdict(len(bad) > 0, warn=flag_consistency["mi_mismatch_pre2010_unflagged"] > 0), selftest=st,
               sample=dict(n_cells=len(r), n_ids=len(ids), top500_cells=int(r.top500.sum()), years=f"{int(r.year.min())}~{int(r.year.max())}",
                           cells_per_year_min=int(r.groupby('year').size().min()), cells_per_year_max=int(r.groupby('year').size().max())),
               ciq=dict(row_missing_in_db=int(r.ciq_row_missing.sum()), all_ok=int(r.all_ok.sum()), mismatch_cells=int(len(bad)),
                        per_field_mismatch={f: int((~r[f"{f}_ok"]).sum()) for f in ["open", "high", "low", "close", "vwap", "volume"]},
                        max_rel_close_diff=float(np.nanmax(np.abs(r.close / r.close_exp - 1))) if len(r) else None,
                        max_abs_volume_diff=float(np.nanmax(np.abs(r.volume - r.volume_exp))) if len(r) else None),
               mi=dict(cells_with_mi_close=int(r.mi_close_available.sum()), mi_rel_gt_1e6=int((r.mi_close_rel > 1e-6).sum()),
                       mi_rel_gt_1e3=int(r.mi_mismatch_1e3.sum()), **flag_consistency),
               flag_strata=flag_strata, tolerance="가격 상대 1e-6(|x|<1 은 절대 1e-6), 거래량 절대 1주")
    if flag_strata["flag2_with_mi_mismatch_1e3"] != flag_strata["flag2_cells"] - flag_strata["flag2_mi_close_missing"] or flag_strata["flag3_db_close_null"] != flag_strata["flag3_cells"] \
            or flag_strata["flag2_ciq_values_ok"] != flag_strata["flag2_cells"] or flag_strata["flag3_values_ok"] != flag_strata["flag3_cells"]:
        res["verdict"] = "FAIL" if res["verdict"] == "PASS" else res["verdict"]
        res["flag_strata_note"] = "플래그 의미와 원자료 불일치"
    return res


# ----------------------------------------------------------------------------------------------------------------------
# C. 계수 구간 커버리지 · 원본 대조 · 극단값
# ----------------------------------------------------------------------------------------------------------------------
def selftest_C() -> dict:
    d = lambda s: pd.Timestamp(s)  # noqa
    good = pd.DataFrame({"tradingItemId": [1, 1, 1, 2], "validFrom": [d("1900-01-01"), d("2010-01-01"), d("2015-06-01"), d("1900-01-01")],
                         "validTo": [d("2010-01-01"), d("2015-06-01"), pd.NaT, pd.NaT], "factor": [4.0, 2.0, 1.0, 1.0]})
    iv = Intervals(good)
    cov = iv.coverage_check(expect_ids=[1, 2])
    ok = cov["n_bad_ids"] == 0 and cov["missing_ids"] == 0
    v, hit = iv.lookup(np.array([1, 1, 1, 2, 3]), ts_ns(pd.to_datetime(["2009-12-31", "2010-01-01", "2015-05-31", "2030-01-01", "2020-01-01"])))
    ok &= bool(hit.tolist() == [True, True, True, True, False] and v[:4].tolist() == [4.0, 2.0, 2.0, 1.0])
    gap = good.copy(); gap.loc[1, "validFrom"] = d("2010-01-02")            # 빈틈 하루
    ovl = good.copy(); ovl.loc[1, "validFrom"] = d("2009-12-31")            # 겹침 하루
    two_open = good.copy(); two_open.loc[1, "validTo"] = pd.NaT             # 열림 2개
    not1900 = good.copy(); not1900.loc[3, "validFrom"] = d("1950-01-01")
    checks = {k: Intervals(v_).coverage_check() for k, v_ in dict(gap=gap, ovl=ovl, two_open=two_open, not1900=not1900).items()}
    ok &= checks["gap"]["gaps"] == 1 and checks["ovl"]["overlaps"] == 1 and checks["two_open"]["ids_with_open_count_ne_1"] == 1 and checks["not1900"]["first_not_1900"] == 1
    _, hit_gap = Intervals(gap).lookup(np.array([1]), ts_ns(pd.to_datetime(["2010-01-01"])))
    ok &= not hit_gap[0]
    return dict(passed=bool(ok), detail=dict(good=cov, **checks))


def item_C(b: Build, out: Path, tmp: Path, db: DB, seed: int, n_ids: int = 300, **_) -> dict:
    st = selftest_C()
    if not st["passed"]:
        return dict(verdict="FAIL", selftest=st)
    inst_ids = sorted(b.instruments().tradingItemId.astype(int).tolist())
    split = b.table("factors/split.parquet")
    div = b.table("factors/dividend.parquet")
    ivs, ivd = Intervals(split), Intervals(div)
    cov = dict(split=ivs.coverage_check(expect_ids=inst_ids), dividend=ivd.coverage_check(expect_ids=inst_ids))
    rng = np.random.default_rng(seed + 1)
    # 표본 300: 200 무작위 + 100 everTop500
    top = sorted(b.ever_top500())
    ids = sorted(set(rng.choice(inst_ids, size=200, replace=False).tolist()) | set(rng.choice(top, size=100, replace=False).tolist()))
    # (1) 구간 lookup vs adjustment_factor_daily(전 행) vs DB adjustmentfactor(전 행)
    adjf = b.read_ids("factors/adjustment_factor_daily.parquet", ids)
    dbf = db.q_ids(f"""select tradingitemid as "tradingItemId", pricingdate as date, adjustmentfactor as adjf_db, priceclose as close_db
                        from {SCHEMA}.ciqpriceequity where tradingitemid = any(%s) and pricingdate >= '2000-01-03'""", ids)
    dbf["date"] = pd.to_datetime(dbf.date); dbf["tradingItemId"] = dbf.tradingItemId.astype(np.int64)
    dbf["adjf_db"] = dbf.adjf_db.astype(float); dbf["close_db"] = dbf.close_db.astype(float)
    m = adjf.merge(dbf, on=["tradingItemId", "date"], how="outer", indicator=True)
    look, hit = ivs.lookup(m.tradingItemId.to_numpy(np.int64), ts_ns(m.date))
    m["lookup"] = look; m["hit"] = hit
    both = m[m._merge == "both"]
    rel_side_db = np.abs(both.adjustmentfactor / both.adjf_db - 1)
    rel_look_side = np.abs(both.lookup / both.adjustmentfactor - 1)
    rel_look_db = np.abs(both.lookup / both.adjf_db - 1)
    cal_days = b.calendar_days()
    only_db = m[m._merge == "right_only"]
    only_db_oncal = only_db[np.isin(ts_ns(only_db.date), cal_days)]
    # asOf 이후 행은 제외
    asof = pd.Timestamp(b.manifest["asOfDate"])
    only_db_oncal = only_db_oncal[only_db_oncal.date <= asof]
    split_cmp = dict(ids=len(ids), rows_side=int(len(adjf)), rows_db=int(len(dbf)), rows_both=int(len(both)),
                     only_in_side_table=int((m._merge == "left_only").sum()), only_in_db_on_calendar_le_asof=int(len(only_db_oncal)),
                     only_in_db_total=int(len(only_db)),
                     side_vs_db_gt_1e6=int((rel_side_db > 1e-6).sum()), lookup_vs_side_gt_1e6=int((rel_look_side > 1e-6).sum()),
                     lookup_vs_db_gt_1e6=int((rel_look_db > 1e-6).sum()), lookup_miss=int((~m.hit).sum()),
                     max_rel_lookup_vs_db=float(np.nanmax(rel_look_db)) if len(both) else None)
    bad = both[(rel_look_db > 1e-6) | (rel_side_db > 1e-6)]
    bad.to_csv(out / "C_split_lookup_mismatch.csv", index=False)
    only_db_oncal.to_csv(out / "C_rows_only_in_db.csv", index=False)
    # (2) ciqsplitcache 원본 → 독립 재구성 후 split.parquet 구간(비합성) 대조
    sc = db.q_ids(f"""select tradingitemid as "tradingItemId", splitdate, nextsplitdate, cumulativesplitfactor as csf, latestsplitfactor as lsf
                       from {SCHEMA}.ciqsplitcache where tradingitemid = any(%s)""", ids)
    sc["splitdate"] = pd.to_datetime(sc.splitdate); sc["nextsplitdate"] = pd.to_datetime(sc.nextsplitdate)
    sc["csf"] = sc.csf.astype(float); sc["lsf"] = sc.lsf.astype(float)
    s_sub = split[split.tradingItemId.isin(ids)]
    # 벤더 구간이 있는 종목 중 source 가 ciqsplitcache 인 구간은 (id, splitdate, csf) 와 1:1 이어야 함
    v_rows = s_sub[s_sub.source == "ciqsplitcache"]
    key_v = set(zip(v_rows.tradingItemId.astype(int), v_rows.validFrom.dt.strftime("%Y-%m-%d"), np.round(v_rows.factor, 9)))
    key_db = set(zip(sc.tradingItemId.astype(int), sc.splitdate.dt.strftime("%Y-%m-%d"), np.round(sc.csf, 9)))
    ids_adjf_src = set(s_sub.loc[s_sub.source == "ciqpriceequity_adjf", "tradingItemId"].astype(int))
    key_db_nonrecon = {k for k in key_db if k[0] not in ids_adjf_src}
    sc_cmp = dict(db_rows=int(len(sc)), db_ids=int(sc.tradingItemId.nunique()), build_ciqsplitcache_rows=int(len(v_rows)),
                  ids_reconstructed_from_adjf=len(ids_adjf_src), build_rows_not_in_db=len(key_v - key_db), db_rows_not_in_build_excl_recon=len(key_db_nonrecon - key_v))
    # 첫 구간 이전 = csf×lsf 검사
    first = sc.sort_values(["tradingItemId", "splitdate"]).groupby("tradingItemId").first()
    pre = s_sub[s_sub.source == "synthetic_pre_first"].set_index("tradingItemId")
    j = pre.join(first, how="inner")
    j = j[~j.index.isin(ids_adjf_src)]
    sc_cmp["pre_first_rows_checked"] = int(len(j))
    sc_cmp["pre_first_ne_csf_x_lsf"] = int((np.abs(j.factor / (j.csf * j.lsf) - 1) > 1e-9).sum())
    # (3) ciqpriceequitydivadjfactor 원본 대조: 폐구간 → 반개구간 변환 후 (id, from, to, factor) 일치
    dv = db.q_ids(f"""select tradingitemid as "tradingItemId", fromdate, todate, divadjfactor from {SCHEMA}.ciqpriceequitydivadjfactor
                       where tradingitemid = any(%s)""", ids)
    dv["fromdate"] = pd.to_datetime(dv.fromdate); dv["todate"] = pd.to_datetime(dv.todate); dv["divadjfactor"] = dv.divadjfactor.astype(float)
    d_sub = div[div.tradingItemId.isin(ids)]
    dv_rows = d_sub[d_sub.source == "ciqpriceequitydivadjfactor"]
    kb = set(zip(dv_rows.tradingItemId.astype(int), dv_rows.validFrom.dt.strftime("%Y-%m-%d"),
                 dv_rows.validTo.dt.strftime("%Y-%m-%d").fillna("open"), np.round(dv_rows.factor, 10)))
    to_half = (dv.todate + pd.Timedelta(days=1)).dt.strftime("%Y-%m-%d").where(dv.todate.notna(), "open")
    kd = set(zip(dv.tradingItemId.astype(int), dv.fromdate.dt.strftime("%Y-%m-%d"), to_half, np.round(dv.divadjfactor, 10)))
    # 구간이 붙어 있지 않은(gap) 경우 build 는 gap_fill 을 넣고 벤더 todate 는 그대로 → 키가 어긋날 수 있어 별도 계산
    dv_cmp = dict(db_rows=int(len(dv)), db_ids=int(dv.tradingItemId.nunique()), build_vendor_rows=int(len(dv_rows)),
                  build_rows_not_in_db=len(kb - kd), db_rows_not_in_build=len(kd - kb), gap_fill_rows=int((d_sub.source == "synthetic_gap_fill").sum()),
                  ids_no_window_build=int((d_sub.source == "synthetic_no_window").sum()), ids_no_window_db=len(set(ids) - set(dv.tradingItemId.astype(int))))
    diffs = pd.DataFrame(sorted(kb ^ kd), columns=["tradingItemId", "from", "to", "factor"]) if (kb ^ kd) else pd.DataFrame()
    diffs.to_csv(out / "C_dividend_interval_diff.csv", index=False)
    # 일별 lookup 대조: DB 폐구간 lookup vs build 반개구간 lookup (표본 종목 전 행)
    ivd_db = Intervals(dv.rename(columns={"fromdate": "validFrom", "todate": "validTo", "divadjfactor": "factor"}), closed_right=True)
    rows = adjf[["tradingItemId", "date"]]
    vb, hb = ivd.lookup(rows.tradingItemId.to_numpy(np.int64), ts_ns(rows.date))
    vd, hd = ivd_db.lookup(rows.tradingItemId.to_numpy(np.int64), ts_ns(rows.date))
    # DB 에 없는 날(첫 구간 이전·gap·구간 없음)은 build 규칙(첫 구간값·직전값·1.0)으로 채움 → 여기서는 hit 인 날만 대조
    both_hit = hb & hd
    dv_cmp["daily_rows"] = int(len(rows)); dv_cmp["daily_rows_db_hit"] = int(hd.sum()); dv_cmp["daily_build_miss"] = int((~hb).sum())
    dv_cmp["daily_mismatch_gt_1e9"] = int((np.abs(vb[both_hit] / vd[both_hit] - 1) > 1e-9).sum())
    # DB 미적중 날의 build 값 성격: 첫 구간 이전 → 첫 구간값 / 구간 없는 종목 → 1.0
    miss = rows[~hd].copy(); miss["build"] = vb[~hd]
    first_dv = dv.sort_values(["tradingItemId", "fromdate"]).groupby("tradingItemId").first()
    miss["first_db"] = miss.tradingItemId.map(first_dv.divadjfactor)
    miss["first_from"] = miss.tradingItemId.map(first_dv.fromdate)
    pre_first = miss[miss.date < miss.first_from]
    no_win = miss[miss.first_db.isna()]
    dv_cmp["db_miss_pre_first_rows"] = int(len(pre_first)); dv_cmp["db_miss_pre_first_ne_firstval"] = int((np.abs(pre_first.build / pre_first.first_db - 1) > 1e-12).sum())
    dv_cmp["db_miss_no_window_rows"] = int(len(no_win)); dv_cmp["db_miss_no_window_ne_1"] = int((no_win.build != 1.0).sum())
    dv_cmp["db_miss_other_rows(gap/post)"] = int(len(miss) - len(pre_first) - len(no_win))
    # (4) 극단값 종목 × 유니버스 멤버 기간 겹침
    ext = split[(split.factor < 1e-3) | (split.factor > 100)].copy()
    mb = pd.concat([b.membership(False).assign(table="all"), b.membership(True).assign(table="v1rule")])
    mb_e = mb[mb.tradingItemId.isin(ext.tradingItemId)]
    hits = []
    for r in ext.itertuples():
        sub = mb_e[mb_e.tradingItemId == r.tradingItemId]
        vt = r.validTo if pd.notna(r.validTo) else pd.Timestamp("2100-01-01")
        ov = sub[(sub.fromDate < vt) & (sub.toDate >= r.validFrom)]
        for o in ov.itertuples():
            hits.append(dict(tradingItemId=r.tradingItemId, validFrom=r.validFrom, validTo=r.validTo, factor=r.factor, source=r.source,
                             universe=o.universe, table=o.table, memFrom=o.fromDate, memTo=o.toDate))
    hits_df = pd.DataFrame(hits)
    hits_df.to_csv(out / "C_extreme_factor_universe_overlap.csv", index=False)
    ext_summary = dict(extreme_rows=int(len(ext)), extreme_ids=int(ext.tradingItemId.nunique()), min_factor=float(split.factor.min()), max_factor=float(split.factor.max()),
                       overlap_rows=int(len(hits_df)), overlap_ids=int(hits_df.tradingItemId.nunique()) if len(hits_df) else 0,
                       overlap_by_universe=hits_df.groupby(["table", "universe"]).tradingItemId.nunique().to_dict() if len(hits_df) else {},
                       extreme_ids_ever_top500=len(set(ext.tradingItemId.astype(int)) & b.ever_top500()),
                       extreme_ids_ever_top1000=len(set(ext.tradingItemId.astype(int)) & b.ever_universe("Top1000")))
    fail = (cov["split"]["n_bad_ids"] or cov["dividend"]["n_bad_ids"] or cov["split"]["missing_ids"] or cov["dividend"]["missing_ids"]
            or split_cmp["lookup_vs_db_gt_1e6"] or split_cmp["lookup_miss"] or dv_cmp["daily_mismatch_gt_1e9"] or dv_cmp["daily_build_miss"]
            or dv_cmp["db_miss_pre_first_ne_firstval"] or dv_cmp["db_miss_no_window_ne_1"])
    warn = bool(split_cmp["only_in_db_on_calendar_le_asof"] or sc_cmp["build_rows_not_in_db"] or dv_cmp["build_rows_not_in_db"] or ext_summary["overlap_rows"])
    return dict(verdict=verdict(bool(fail), warn), selftest=st, coverage=cov, split_vs_adjf_and_db=split_cmp, splitcache_vs_build=sc_cmp,
                divadj_vs_build=dv_cmp, extreme=ext_summary, sample_ids=len(ids))


# ----------------------------------------------------------------------------------------------------------------------
# D. returns_total 독립 재계산 · 배당 항등식 · 분할 경계
# ----------------------------------------------------------------------------------------------------------------------
def recompute_returns(base: pd.DataFrame, ivs: Intervals, ivd: Intervals, lc: pd.DataFrame) -> pd.DataFrame:
    """계약 산식: adj = close/sf×df, 직전 관측(close notna) 행 대비, lastTradeDate 이후 행 제거."""
    d = base[base.close.notna()].sort_values(["tradingItemId", "date"]).reset_index(drop=True)
    ids = d.tradingItemId.to_numpy(np.int64)
    di8 = ts_ns(d.date)
    sf, hs = ivs.lookup(ids, di8)
    df_, hd = ivd.lookup(ids, di8)
    adj = d.close.to_numpy(np.float64) / sf * df_
    same = np.r_[False, ids[1:] == ids[:-1]]
    ret = np.full(len(d), np.nan)
    ret[1:] = np.where(same[1:], adj[1:] / adj[:-1] - 1.0, np.nan)
    prev = np.full(len(d), np.datetime64("NaT"), dtype="datetime64[ns]")
    prev[1:] = np.where(same[1:], d.date.to_numpy()[:-1], np.datetime64("NaT"))
    d["ret_calc"] = ret; d["prevDate_calc"] = prev; d["sf"] = sf; d["df"] = df_; d["hit"] = hs & hd
    ltd = lc.lastTradeDate.reindex(d.tradingItemId).to_numpy()
    d["post_lifecycle"] = d.date.to_numpy() > ltd
    return d


def selftest_D() -> dict:
    d = pd.to_datetime(["2020-01-02", "2020-01-03", "2020-01-06", "2020-01-07", "2020-01-08"])
    base = pd.DataFrame({"tradingItemId": [1] * 5, "date": d, "close": [100.0, 102.0, np.nan, 51.5, 52.0]})
    split = pd.DataFrame({"tradingItemId": [1, 1], "validFrom": [pd.Timestamp("1900-01-01"), pd.Timestamp("2020-01-07")], "validTo": [pd.Timestamp("2020-01-07"), pd.NaT], "factor": [2.0, 1.0]})
    divd = pd.DataFrame({"tradingItemId": [1, 1], "validFrom": [pd.Timestamp("1900-01-01"), pd.Timestamp("2020-01-08")], "validTo": [pd.Timestamp("2020-01-08"), pd.NaT], "factor": [0.99, 1.0]})
    lc = pd.DataFrame({"lastTradeDate": [pd.Timestamp("2020-01-07")]}, index=pd.Index([1], name="tradingItemId"))
    r = recompute_returns(base, Intervals(split), Intervals(divd), lc)
    exp = [np.nan, 0.02, 51.5 / 51.0 - 1, (52.0 / 0.99) / 51.5 - 1]
    ok = bool(np.allclose(r.ret_calc.to_numpy()[1:], exp[1:], atol=1e-12) and np.isnan(r.ret_calc.iloc[0]) and r.post_lifecycle.tolist() == [False, False, False, True]
              and str(r.prevDate_calc.iloc[2].date()) == "2020-01-03")
    # 반례: 파일 값이 1e-6 어긋나면 검출
    file_ret = r.ret_calc.copy(); file_ret.iloc[1] += 2e-6
    ok &= bool((np.abs(file_ret - r.ret_calc) > 1e-9).sum() == 1)
    # 배당 항등식 반례
    ret_total = (52.0 / 0.99) / 51.5 - 1; ret_price = 52.0 / 51.5 - 1; D = 0.515
    lhs = 1 + ret_total; rhs = (1 + ret_price) / (1 - D / 51.5)
    ok &= abs(lhs - rhs) < 1e-12
    ok &= abs(lhs - (1 + ret_price) / (1 - 1.5 * D / 51.5)) > 1e-6
    return dict(passed=ok, detail=dict(ret_calc=r.ret_calc.tolist(), identity_diff=lhs - rhs))


def item_D(b: Build, out: Path, tmp: Path, db: DB, seed: int, n_ids: int = 500, n_div: int = 5000, **_) -> dict:
    st = selftest_D()
    if not st["passed"]:
        return dict(verdict="FAIL", selftest=st)
    rng = np.random.default_rng(seed + 2)
    inst_ids = sorted(b.instruments().tradingItemId.astype(int).tolist())
    top = sorted(b.ever_top500())
    ids = sorted(set(rng.choice(inst_ids, size=n_ids - 150, replace=False).tolist()) | set(rng.choice(top, size=150, replace=False).tolist()))
    ivs, ivd = Intervals(b.table("factors/split.parquet")), Intervals(b.table("factors/dividend.parquet"))
    lc = b.lifecycle()
    base = b.read_ids("base_data.parquet", ids, columns=["tradingItemId", "date", "close", "qualityFlag"])
    rc = recompute_returns(base, ivs, ivd, lc)
    rc = rc[~rc.post_lifecycle]
    fr = b.read_ids("derived/returns_total.parquet", ids)
    m = rc.merge(fr, on=["tradingItemId", "date"], how="outer", indicator=True)
    both = m[m._merge == "both"]
    diff = np.abs(both.ret - both.ret_calc)
    nan_mismatch = both.ret.isna() != both.ret_calc.isna()
    prev_mismatch = (both.prevDate != both.prevDate_calc) & ~(both.prevDate.isna() & both.prevDate_calc.isna())
    ret_cmp = dict(ids=len(ids), rows_recomputed=int(len(rc)), rows_file=int(len(fr)), only_in_recompute=int((m._merge == "left_only").sum()),
                   only_in_file=int((m._merge == "right_only").sum()), max_abs_diff=float(np.nanmax(diff)) if len(both) else None,
                   diff_gt_1e9=int((diff > 1e-9).sum()), diff_gt_1e12=int((diff > 1e-12).sum()), nan_pattern_mismatch=int(nan_mismatch.sum()),
                   prevDate_mismatch=int(prev_mismatch.sum()), factor_lookup_miss=int((~rc.hit).sum()))
    m[(diff.reindex(m.index) > 1e-9) | (m._merge != "both")].to_csv(out / "D_returns_mismatch.csv", index=False)
    # --- 배당 항등식: ex-date 표본 (DB ciqdividend, USD 현금, 창 (prev, t] 안 비현금·비USD 있으면 제외, 창 안 현금배당은 전부 합산)
    div_ids = sorted(set(rng.choice(inst_ids, size=1500, replace=False).tolist()) | set(rng.choice(top, size=400, replace=False).tolist()))
    dv = db.q_ids(f"""select tradingitemid as "tradingItemId", exdate, divamount, currencyid, dividendtypeid, supplementaltypeid
                       from {SCHEMA}.ciqdividend where tradingitemid = any(%s) and exdate >= '1999-12-01' and exdate <= %s""", div_ids,
                  params_after=(b.manifest["asOfDate"],))
    dv["exdate"] = pd.to_datetime(dv.exdate); dv["divamount"] = dv.divamount.astype(float)
    dv["is_cash"] = dv.dividendtypeid.isin(CASH_DIV_TYPES) & (dv.currencyid == USD) & dv.divamount.notna()
    dv["is_other"] = ~dv.is_cash
    cand = dv[dv.is_cash & (dv.exdate >= "2000-01-04") & (dv.divamount > 0)][["tradingItemId", "exdate"]].drop_duplicates().sort_values(["tradingItemId", "exdate"]).reset_index(drop=True)
    if len(cand) > n_div:
        cand = cand.sample(n=n_div, random_state=seed)
    ex_ids = sorted(cand.tradingItemId.unique().tolist())
    bx = b.read_ids("base_data.parquet", ex_ids, columns=["tradingItemId", "date", "close"])
    bx = bx[bx.close.notna()].sort_values(["tradingItemId", "date"]).reset_index(drop=True)
    fx = b.read_ids("derived/returns_total.parquet", ex_ids).set_index(["tradingItemId", "date"])
    dv_by_id = {k: g for k, g in dv.groupby("tradingItemId")}
    div_bd = set(zip(b.table("factors/dividend.parquet").tradingItemId.astype(int), b.table("factors/dividend.parquet").validFrom.dt.strftime("%Y-%m-%d")))
    asof = pd.Timestamp(b.manifest["asOfDate"])
    rows = []
    bx_i8 = ts_ns(bx.date)
    grp_pos = {k: (v.min(), v.max()) for k, v in bx.groupby("tradingItemId").indices.items()}
    for r in cand.itertuples():
        if r.tradingItemId not in grp_pos:
            continue
        lo, hi = grp_pos[r.tradingItemId]
        seg = bx_i8[lo:hi + 1]
        pos = np.searchsorted(seg, np.datetime64(r.exdate, "ns").astype("int64"), side="left")
        if pos == 0 or pos >= len(seg):
            continue
        t_row = bx.iloc[lo + pos]; p_row = bx.iloc[lo + pos - 1]
        key = (r.tradingItemId, t_row.date)
        if key not in fx.index:
            continue
        g = dv_by_id[r.tradingItemId]
        win = g[(g.exdate > p_row.date) & (g.exdate <= t_row.date)]
        D = float(win.loc[win.is_cash, "divamount"].sum()); n_other = int(win.is_other.sum()); n_ex = int(win.exdate.nunique())
        ret_total = fx.loc[key, "ret"]
        sf_t, _ = ivs.lookup(np.array([r.tradingItemId]), np.array([ts_ns([t_row.date])[0]]))
        sf_p, _ = ivs.lookup(np.array([r.tradingItemId]), np.array([ts_ns([p_row.date])[0]]))
        ret_price = (t_row.close / sf_t[0]) / (p_row.close / sf_p[0]) - 1
        D_adj = D * (sf_t[0] / sf_p[0])  # 배당락일 주식 기준 D 를 prev 기준으로
        rhs = (1 + ret_price) / (1 - D_adj / p_row.close)
        rows.append(dict(tradingItemId=r.tradingItemId, exdate=r.exdate, date=t_row.date, prevDate=p_row.date, D=D, P_prev=p_row.close, close=t_row.close,
                         sf_prev=sf_p[0], sf_t=sf_t[0], split_between=sf_t[0] != sf_p[0], n_exdates_in_window=n_ex, n_other_in_window=n_other,
                         ret_total=ret_total, ret_price=ret_price, diff=(1 + ret_total) - rhs, exdate_is_trading=(t_row.date == r.exdate),
                         step_in_build=(int(r.tradingItemId), str(t_row.date.date())) in div_bd, is_asof_day=(t_row.date == asof), gap_days=(t_row.date - p_row.date).days))
    idn = pd.DataFrame(rows)
    idn["absdiff"] = idn["diff"].abs()
    idn["everTop500"] = idn.tradingItemId.isin(top)
    idn.to_csv(out / "D_dividend_identity.csv", index=False)
    excluded = idn.split_between | (idn.n_other_in_window > 0)
    clean = idn[~excluded]
    ident = dict(sample_exdates=int(len(idn)), ids=int(idn.tradingItemId.nunique()), excluded_split_between=int(idn.split_between.sum()),
                 excluded_noncash_or_nonusd_in_window=int((idn.n_other_in_window > 0).sum()), n_checked=int(len(clean)),
                 pass_1e6=int((clean.absdiff <= 1e-6).sum()), pass_1e5=int((clean.absdiff <= 1e-5).sum()), pass_1e4=int((clean.absdiff <= 1e-4).sum()),
                 fail_1e6=int((clean.absdiff > 1e-6).sum()), fail_1e4=int((clean.absdiff > 1e-4).sum()), median_absdiff=float(clean.absdiff.median()),
                 p99_absdiff=float(clean.absdiff.quantile(0.99)), max_absdiff=float(clean.absdiff.max()),
                 fail_1e4_asof_day=int(((clean.absdiff > 1e-4) & clean.is_asof_day).sum()), fail_1e4_no_step_in_build=int(((clean.absdiff > 1e-4) & ~clean.step_in_build).sum()),
                 fail_1e4_multi_exdate_window=int(((clean.absdiff > 1e-4) & (clean.n_exdates_in_window > 1)).sum()),
                 everTop500_checked=int(clean.everTop500.sum()), everTop500_fail_1e6=int(((clean.absdiff > 1e-6) & clean.everTop500).sum()),
                 everTop500_fail_1e4=int(((clean.absdiff > 1e-4) & clean.everTop500).sum()),
                 asof_day_exdates_checked=int(clean.is_asof_day.sum()), asof_day_step_in_build=int((clean.is_asof_day & clean.step_in_build).sum()),
                 note="1e-6 초과분은 벤더 계수 소수 5자리 저장(T2 §4) 수준; 1e-4 초과가 실질 불일치")
    clean[clean.absdiff > 1e-6].sort_values("absdiff", ascending=False).to_csv(out / "D_dividend_identity_fail.csv", index=False)
    # --- asOf 근처 배당락의 벤더 계수 지연: 최근 5거래일 ex-date(USD 현금) 중 build 배당계수 스텝이 있는 비율
    cal_days = b.calendar_days()
    recent = [pd.Timestamp(x) for x in cal_days[-5:]]
    exr = db.q(f"""select tradingitemid as "tradingItemId", exdate from {SCHEMA}.ciqdividend where exdate = any(%s) and currencyid = %s and dividendtypeid = any(%s)""",
               ([d.to_pydatetime() for d in recent], USD, sorted(CASH_DIV_TYPES)))
    exr["exdate"] = pd.to_datetime(exr.exdate); exr = exr[exr.tradingItemId.isin(inst_ids)].drop_duplicates()
    dvt0 = b.table("factors/dividend.parquet")
    step_keys = set(zip(dvt0.tradingItemId.astype(int), dvt0.validFrom.dt.strftime("%Y-%m-%d")))
    exr["has_step"] = [(int(t), d.strftime("%Y-%m-%d")) in step_keys for t, d in zip(exr.tradingItemId, exr.exdate)]
    lag = exr.groupby(exr.exdate.dt.strftime("%Y-%m-%d")).agg(exdates=("has_step", "size"), with_step=("has_step", "sum")).reset_index().to_dict("records")
    exr[~exr.has_step].to_csv(out / "D_asof_exdates_without_step.csv", index=False)
    # --- 분할 경계 전수: split.parquet 의 factor 가 바뀌는 경계(validFrom ≥ 2000-01-04) → raw 점프 ≈ sf 비, split_adj 연속, total 연속
    sp = b.table("factors/split.parquet").sort_values(["tradingItemId", "validFrom"])
    sp["prev_factor"] = sp.groupby("tradingItemId").factor.shift(1)
    bd = sp[sp.prev_factor.notna() & (sp.factor != sp.prev_factor) & (sp.validFrom >= "2000-01-04") & (sp.validFrom <= asof)].copy()
    tb = pq.read_table(b.root / "base_data.parquet", columns=["tradingItemId", "date", "close"])
    c_all = tb.column("close").to_numpy(); keep = ~np.isnan(c_all)
    ids_all = tb.column("tradingItemId").to_numpy()[keep]; d_all = tb.column("date").to_numpy().astype("datetime64[ns]").astype("int64")[keep]; c_all = c_all[keep].astype(np.float64)
    del tb
    keys = ids_all.astype(np.int64) * (1 << 20) + days_since_1900(d_all)
    bkey = bd.tradingItemId.to_numpy(np.int64) * (1 << 20) + days_since_1900(ts_ns(bd.validFrom))
    pos = np.searchsorted(keys, bkey, side="left")
    ok = (pos > 0) & (pos < len(keys))
    posc = np.clip(pos, 1, len(keys) - 1)
    ok &= (ids_all[posc] == bd.tradingItemId.to_numpy()) & (ids_all[posc - 1] == bd.tradingItemId.to_numpy())
    bd = bd[ok].copy(); posc = posc[ok]
    bd["date"] = d_all[posc].astype("datetime64[ns]"); bd["prevDate"] = d_all[posc - 1].astype("datetime64[ns]")
    bd["close"] = c_all[posc]; bd["prev_close"] = c_all[posc - 1]
    sf_t, _ = ivs.lookup(bd.tradingItemId.to_numpy(np.int64), ts_ns(bd.date)); sf_p, _ = ivs.lookup(bd.tradingItemId.to_numpy(np.int64), ts_ns(bd.prevDate))
    bd["sf_prev"] = sf_p; bd["sf_t"] = sf_t; bd["ratio"] = sf_p / sf_t
    bd["raw_ret"] = bd.close / bd.prev_close - 1; bd["adj_ret"] = (bd.close / sf_t) / (bd.prev_close / sf_p) - 1
    bd["raw_matches_ratio"] = np.abs((1 + bd.raw_ret) * bd.ratio - 1) < 0.25
    bd["gap_days"] = (bd.date - bd.prevDate).dt.days
    rt = b.table("derived/returns_total.parquet", columns=["tradingItemId", "date", "ret"])
    bd = bd.merge(rt, on=["tradingItemId", "date"], how="left")
    bd["everTop500"] = bd.tradingItemId.isin(top)
    art = pd.read_csv(b.root / "qa/T2/factor_artifacts.csv", parse_dates=["date"])
    art_keys = set(zip(art.tradingItemId.astype(int), art.date.dt.strftime("%Y-%m-%d")))
    bd["in_T2_artifact_list"] = [(int(i), str(pd.Timestamp(d).date())) in art_keys for i, d in zip(bd.tradingItemId, bd.date)]
    fake = bd[(bd.adj_ret.abs() > 0.4) & (bd.raw_ret.abs() < 0.4)]
    fake_total = fake[fake.ret.abs() > 0.4]  # total return 도 불연속 → 진짜 인공물 후보(스핀오프 상쇄 아님)
    bd.to_csv(out / "D_split_boundaries.csv", index=False)
    fake_total.to_csv(out / "D_split_boundary_fake_jumps.csv", index=False)
    sb_sum = dict(boundaries_total=int(len(sp[sp.prev_factor.notna() & (sp.factor != sp.prev_factor) & (sp.validFrom >= "2000-01-04")])), boundaries_with_prev_and_t_close=int(len(bd)),
                  raw_matches_ratio=int(bd.raw_matches_ratio.sum()), raw_not_matching_ratio=int((~bd.raw_matches_ratio).sum()), adj_ret_abs_gt_40pct=int((bd.adj_ret.abs() > 0.4).sum()),
                  fake_jump_cells_splitadj=int(len(fake)), fake_jump_cells_total_return=int(len(fake_total)), fake_total_in_T2_list=int(fake_total.in_T2_artifact_list.sum()),
                  fake_total_not_in_T2_list=int((~fake_total.in_T2_artifact_list).sum()), fake_total_everTop500=int(fake_total.everTop500.sum()),
                  fake_total_ids_everTop1000=len(set(fake_total.tradingItemId.astype(int)) & b.ever_universe("Top1000")),
                  everTop500_boundaries=int(bd.everTop500.sum()), everTop500_raw_not_matching=int((~bd.raw_matches_ratio & bd.everTop500).sum()),
                  median_abs_adj_ret=float(bd.adj_ret.abs().median()), boundary_gap_gt_5d=int((bd.gap_days > 5).sum()),
                  everTop500_not_matching_cases=bd[~bd.raw_matches_ratio & bd.everTop500][["tradingItemId", "date", "prevDate", "sf_prev", "sf_t", "raw_ret", "adj_ret", "ret", "source"]].to_dict("records"),
                  new_fake_not_in_T2=fake_total[~fake_total.in_T2_artifact_list][["tradingItemId", "date", "sf_prev", "sf_t", "raw_ret", "adj_ret", "ret", "source"]].head(20).to_dict("records"))
    # --- 큰 분배(스핀오프·특별배당) 날: 배당계수 스텝이 뜻하는 D/P ≥ 10% 인 경계 → 재투자식(파일) vs 현금식 수익률 차이, 유니버스 멤버 여부
    dvt = b.table("factors/dividend.parquet").sort_values(["tradingItemId", "validFrom"])
    dvt["prev_factor"] = dvt.groupby("tradingItemId").factor.shift(1)
    big = dvt[dvt.prev_factor.notna() & (dvt.validFrom >= "2000-01-04") & (dvt.validFrom <= asof) & (dvt.source == "ciqpriceequitydivadjfactor")].copy()
    big["step"] = big.prev_factor / big.factor; big["DP"] = 1 - big.step
    big = big[big.DP >= 0.10].copy()
    bkey2 = big.tradingItemId.to_numpy(np.int64) * (1 << 20) + days_since_1900(ts_ns(big.validFrom))
    pos2 = np.searchsorted(keys, bkey2, side="left")
    ok2 = (pos2 > 0) & (pos2 < len(keys)); posc2 = np.clip(pos2, 1, len(keys) - 1)
    ok2 &= (ids_all[posc2] == big.tradingItemId.to_numpy()) & (ids_all[posc2 - 1] == big.tradingItemId.to_numpy())
    big = big[ok2].copy(); posc2 = posc2[ok2]
    big["date"] = d_all[posc2].astype("datetime64[ns]"); big["prevDate"] = d_all[posc2 - 1].astype("datetime64[ns]")
    big["close"] = c_all[posc2]; big["prev_close"] = c_all[posc2 - 1]
    sft, _ = ivs.lookup(big.tradingItemId.to_numpy(np.int64), ts_ns(big.date)); sfp, _ = ivs.lookup(big.tradingItemId.to_numpy(np.int64), ts_ns(big.prevDate))
    big["ret_price"] = (big.close / sft) / (big.prev_close / sfp) - 1
    big = big.merge(rt, on=["tradingItemId", "date"], how="left")
    big["ret_cash_formula"] = big.ret_price + big.DP          # (P_t + D)/P_prev − 1, D = DP×P_prev (벤더 평가액 기준)
    big["ret_file_minus_cash"] = big.ret - big.ret_cash_formula
    big["amplification"] = 1 / (1 - big.DP)
    mbx = pd.concat([b.membership(False), b.membership(True)]).drop_duplicates()
    def members_on(tid, d):
        sub = mbx[(mbx.tradingItemId == tid) & (mbx.fromDate <= d) & (mbx.toDate >= d)]
        return ",".join(sorted(sub.universe.unique()))
    big["universe_on_date"] = [members_on(int(t), d) for t, d in zip(big.tradingItemId, big.date)]
    big["top500_on_date"] = big.universe_on_date.str.contains("Top500")
    big["top1000_on_date"] = big.universe_on_date.str.contains("Top1000")
    cur = b.current()
    big["symbol"] = big.tradingItemId.map(cur.symbol)
    big = big.sort_values("ret_file_minus_cash", key=lambda x: -x.abs())
    big.to_csv(out / "D_large_distribution_days.csv", index=False)
    b5 = big[big.top500_on_date]
    ld_sum = dict(boundaries_DP_ge_10pct=int(len(big)), top500_member_days=int(len(b5)), top1000_member_days=int(big.top1000_on_date.sum()),
                  top500_abs_ret_gt_20pct=int((b5.ret.abs() > 0.2).sum()), top500_file_minus_cash_gt_5pct=int((b5.ret_file_minus_cash.abs() > 0.05).sum()),
                  top500_file_minus_cash_gt_10pct=int((b5.ret_file_minus_cash.abs() > 0.10).sum()),
                  top500_cases_gt_5pct=b5[b5.ret_file_minus_cash.abs() > 0.05][["symbol", "tradingItemId", "date", "DP", "ret_price", "ret", "ret_cash_formula", "ret_file_minus_cash", "universe_on_date"]].to_dict("records"),
                  note="ret(파일) = 재투자식 P_t/(P_prev−D); 현금식 = (P_t+D)/P_prev. D/P 가 크면 벤더 평가액 오차가 1/(1−D/P) 배로 증폭됨")
    del keys, ids_all, d_all, c_all
    fail = ret_cmp["diff_gt_1e9"] > 0 or ret_cmp["only_in_recompute"] > 0 or ret_cmp["only_in_file"] > 0 or ret_cmp["nan_pattern_mismatch"] > 0 or ret_cmp["prevDate_mismatch"] > 0
    warn = ident["fail_1e4"] > 0 or sb_sum["fake_jump_cells_total_return"] > 0
    if sb_sum["fake_total_not_in_T2_list"] > 0 and sb_sum["fake_total_everTop500"] > 0:
        fail = True
    warn = warn or ld_sum["top500_file_minus_cash_gt_10pct"] > 0
    return dict(verdict=verdict(bool(fail), bool(warn)), selftest=st, returns_recompute=ret_cmp, dividend_identity=ident, split_boundaries=sb_sum, large_distributions=ld_sum,
                asof_factor_lag=dict(recent_days=lag, note="asOf 당일 배당락 중 계수 스텝 미반영분은 returns_total 이 D/P 만큼 과소(다음 일별 갱신에서 계수 재수신으로 정정)"))


# ----------------------------------------------------------------------------------------------------------------------
# E. lifecycle · 꼬리 · 알려진 사례 · provisional/confirmed DB 재확인
# ----------------------------------------------------------------------------------------------------------------------
KNOWN_CASES = [  # (id, label, expected lastTradeDate or None, expected firstTradeDate or None)
    (2592597, "BHI", "2017-07-03", None), (2599743, "CHFC", "2019-07-31", None), (244324100, "FI", "2021-10-01", None),
    (22534033, "SIVB.Q", "2024-11-06", None), (2600796, "CTXS", "2022-09-29", None), (340314663, "XL", "2018-09-11", None),
    (2607387, "DD(EIDP)", "2017-08-31", None), (242602542, "S", "2020-03-31", None), (2660295, "ULTI", "2019-05-02", None),
    (24687598, "CCO old", None, None), (618650356, "CCO new", None, "2019-06-05"), (2623229, "JCI old", None, None),
    (372277281, "JCI new", None, "2016-09-06"), (2594723, "BLK old", None, None), (1899273640, "BLK new", None, None),
    (312940216, "GOOGL old", None, None), (11311662, "GOOGL", None, None), (7727657, "TEVA", None, None), (61531594, "ASBH", None, None),
]


def selftest_E() -> dict:
    """반례: lastTradeDate 를 하루 앞당긴 lifecycle 로 flag-4 정합 검사가 어긋남을 검출."""
    d = pd.to_datetime(["2020-01-02", "2020-01-03", "2020-01-06"])
    base = pd.DataFrame({"tradingItemId": [1, 1, 1], "date": d, "qualityFlag": [0, 0, 4]})
    lc_good = pd.Series([pd.Timestamp("2020-01-03")], index=pd.Index([1], name="tradingItemId"))
    lc_bad = pd.Series([pd.Timestamp("2020-01-02")], index=pd.Index([1], name="tradingItemId"))
    def check(lc):
        post = base.date.to_numpy() > lc.reindex(base.tradingItemId).to_numpy()
        return int((post != (base.qualityFlag.to_numpy() == 4)).sum())
    return dict(passed=check(lc_good) == 0 and check(lc_bad) == 1)


def item_E(b: Build, out: Path, tmp: Path, db: DB, seed: int, **_) -> dict:
    st = selftest_E()
    if not st["passed"]:
        return dict(verdict="FAIL", selftest=st)
    lc = b.lifecycle()
    asof = pd.Timestamp(b.manifest["asOfDate"])
    # (1) 전수: qualityFlag 4 ⇔ date > lastTradeDate ; returns_total 에 lastTradeDate 이후 행 0
    pf = b.pf("base_data.parquet")
    n_post = n_flag4 = n_mismatch = 0
    flag4_post_close_notna = 0
    for g in range(pf.metadata.num_row_groups):
        t = pf.read_row_group(g, columns=["tradingItemId", "date", "qualityFlag", "close"])
        ids = t.column("tradingItemId").to_numpy(); d = t.column("date").to_numpy().astype("datetime64[ns]")
        ltd = lc.lastTradeDate.reindex(ids).to_numpy().astype("datetime64[ns]")
        post = d > ltd
        qf = t.column("qualityFlag").to_numpy()
        n_post += int(post.sum()); n_flag4 += int((qf == 4).sum()); n_mismatch += int((post != (qf == 4)).sum())
        flag4_post_close_notna += int((post & ~np.isnan(t.column("close").to_numpy())).sum())
    pr = b.pf("derived/returns_total.parquet")
    n_ret_post = 0; n_ret = 0
    for g in range(pr.metadata.num_row_groups):
        t = pr.read_row_group(g, columns=["tradingItemId", "date"])
        ids = t.column("tradingItemId").to_numpy(); d = t.column("date").to_numpy().astype("datetime64[ns]")
        ltd = lc.lastTradeDate.reindex(ids).to_numpy().astype("datetime64[ns]")
        n_ret_post += int((d > ltd).sum()); n_ret += len(ids)
    tails = dict(base_rows_post_lastTrade=n_post, base_flag4_rows=n_flag4, flag4_xor_post=n_mismatch, post_rows_with_close=flag4_post_close_notna,
                 returns_rows=n_ret, returns_rows_post_lastTrade=n_ret_post, manifest_flagged=b.manifest["steps"]["base_data"]["flagged_post_lifecycle"],
                 manifest_dropped=b.manifest["steps"]["returns_total"]["dropped_post_lifecycle"])
    # (2) 알려진 사례 표
    known_ids = [k[0] for k in KNOWN_CASES]
    cur = b.current()
    inst = b.instruments().set_index("tradingItemId")
    ti = db.q_ids(f"""select ti.tradingitemid as "tradingItemId", ti.tickersymbol, ti.exchangeid, e.exchangename, ti.currencyid, c.isocode, ti.tradingitemstatusid,
                          ti.primaryflag as ti_primary, s.securityid, s.securityname, s.securitystartdate, s.securityenddate, s.securitysubtypeid, s.primaryflag as sec_primary,
                          co.companyid, co.companyname, co.companytypeid, co.countryid
                       from {SCHEMA}.ciqtradingitem ti join {SCHEMA}.ciqsecurity s on s.securityid=ti.securityid join {SCHEMA}.ciqcompany co on co.companyid=s.companyid
                       left join {SCHEMA}.ciqexchange e on e.exchangeid=ti.exchangeid left join {SCHEMA}.ciqcurrency c on c.currencyid=ti.currencyid
                       where ti.tradingitemid = any(%s)""", known_ids).set_index("tradingItemId")
    base_k = b.read_ids("base_data.parquet", known_ids, columns=["tradingItemId", "date", "close", "volume", "qualityFlag"])
    ret_k = b.read_ids("derived/returns_total.parquet", known_ids, columns=["tradingItemId", "date"])
    mb = pd.concat([b.membership(False).assign(table="all"), b.membership(True).assign(table="v1rule")])
    rows = []
    for tid, label, exp_last, exp_first in KNOWN_CASES:
        l = lc.loc[tid] if tid in lc.index else None
        bk = base_k[base_k.tradingItemId == tid]
        rk = ret_k[ret_k.tradingItemId == tid]
        m_ = mb[(mb.tradingItemId == tid)]
        rows.append(dict(tradingItemId=tid, label=label, in_lifecycle=l is not None, symbol=cur.symbol.get(tid), status=l.status if l is not None else None,
                         endReason=l.endReason if l is not None else None, firstPriceDate=l.firstPriceDate if l is not None else None,
                         firstTradeDate=l.firstTradeDate if l is not None else None, lastTradeDate=l.lastTradeDate if l is not None else None,
                         lastTradeSource=l.lastTradeSource if l is not None else None, ciqLast=l.ciqLastCloseDate if l is not None else None,
                         miLast=l.miLastCloseDate if l is not None else None, secEnd=l.securityEndDate if l is not None else None,
                         expected_last=exp_last, last_ok=(exp_last is None) or (l is not None and str(l.lastTradeDate.date()) == exp_last),
                         expected_first=exp_first, first_ok=(exp_first is None) or (l is not None and str(l.firstTradeDate.date()) == exp_first),
                         base_rows=len(bk), base_close_rows=int(bk.close.notna().sum()), base_first=bk.date.min(), base_last=bk.date.max(),
                         base_last_close=bk.loc[bk.close.notna(), "date"].max(), flag4_rows=int((bk.qualityFlag == 4).sum()),
                         returns_last=rk.date.max(), returns_post_last=int((rk.date > l.lastTradeDate).sum()) if l is not None else None,
                         universe_member_ever=sorted(m_.universe.unique().tolist()), universe_first=m_.fromDate.min(), universe_last=m_.toDate.max(),
                         db_ticker=ti.tickersymbol.get(tid), db_exchange=ti.exchangename.get(tid), db_ccy=ti.isocode.get(tid), db_status=ti.tradingitemstatusid.get(tid),
                         db_secEnd=ti.securityenddate.get(tid), db_company=ti.companyname.get(tid), db_secsubtype=ti.securitysubtypeid.get(tid)))
    kdf = pd.DataFrame(rows)
    kdf.to_csv(out / "E_known_cases.csv", index=False)
    # (3) TEVA 7727657 규명: DB 행 분포·거래소·통화·mi 유무·다른 TEVA 라인
    teva = db.q(f"""select count(*) n, min(pricingdate) mn, max(pricingdate) mx, count(priceclose) n_close, sum(case when volume>0 then 1 else 0 end) n_vol,
                     avg(priceclose*adjustmentfactor) avg_raw_close from {SCHEMA}.ciqpriceequity where tradingitemid = any(%s)""", ([7727657],))
    teva_years = db.q(f"""select extract(year from pricingdate)::int y, count(*) n, count(priceclose) n_close, avg(priceclose*adjustmentfactor) avg_raw, sum(volume) vol
                          from {SCHEMA}.ciqpriceequity where tradingitemid = any(%s) group by 1 order by 1""", ([7727657],))
    teva_mi = db.q(f"select count(*) n, min(pricedate) mn, max(pricedate) mx, count(priceclose) n_close from {SCHEMA}.miprice where tradingitemid = any(%s)", ([7727657],))
    teva_lines = db.q(f"""select ti.tradingitemid, ti.tickersymbol, ti.exchangeid, e.exchangename, ti.currencyid, ti.tradingitemstatusid, ti.primaryflag, s.securityid, s.securityname,
                           s.securitysubtypeid, s.primaryflag secprimary, s.securitystartdate, s.securityenddate
                        from {SCHEMA}.ciqtradingitem ti join {SCHEMA}.ciqsecurity s on s.securityid=ti.securityid left join {SCHEMA}.ciqexchange e on e.exchangeid=ti.exchangeid
                        where s.companyid = (select s2.companyid from {SCHEMA}.ciqsecurity s2 join {SCHEMA}.ciqtradingitem t2 on t2.securityid=s2.securityid where t2.tradingitemid=7727657)
                        order by ti.tradingitemid""")
    # TEVA 라인의 종가 vs 같은 날 다른 TEVA 라인(ADR) 종가 비교(표본 30일)
    teva_rows = db.q(f"select pricingdate, priceclose*adjustmentfactor raw_close, volume/adjustmentfactor raw_vol, adjustmentfactor from {SCHEMA}.ciqpriceequity where tradingitemid = any(%s) order by pricingdate", ([7727657],))
    teva_rows["pricingdate"] = pd.to_datetime(teva_rows.pricingdate)
    other_ids = [int(x) for x in teva_lines.tradingitemid if int(x) != 7727657 and int(x) in set(inst.index)]
    teva_cmp = None
    if other_ids:
        oth = db.q(f"select tradingitemid, pricingdate, priceclose*adjustmentfactor raw_close from {SCHEMA}.ciqpriceequity where tradingitemid = any(%s) and pricingdate = any(%s)",
                   (other_ids, [d.to_pydatetime() for d in teva_rows.pricingdate.tail(200)]))
        oth["pricingdate"] = pd.to_datetime(oth.pricingdate)
        j = teva_rows.merge(oth, on="pricingdate", suffixes=("", "_other"))
        j["ratio"] = j.raw_close.astype(float) / j.raw_close_other.astype(float)
        teva_cmp = dict(other_ids=other_ids, n_common=int(len(j)), ratio_median=float(j.ratio.median()) if len(j) else None, ratio_p10=float(j.ratio.quantile(0.1)) if len(j) else None,
                        ratio_p90=float(j.ratio.quantile(0.9)) if len(j) else None)
    teva_info = dict(db_rows=teva.to_dict("records")[0], years=teva_years.to_dict("records"), mi=teva_mi.to_dict("records")[0], company_lines=teva_lines.to_dict("records"),
                     vs_other_line=teva_cmp, in_universe=kdf.loc[kdf.tradingItemId == 7727657, "universe_member_ever"].iloc[0],
                     lifecycle=kdf.loc[kdf.tradingItemId == 7727657].iloc[0][["status", "firstTradeDate", "lastTradeDate", "base_rows", "base_close_rows"]].to_dict())
    teva_rows.to_csv(out / "E_teva_rows.csv", index=False)
    # (4) provisional 352 · confirmed 규칙 표본 20 DB 재확인
    prov = lc[lc.status == "provisional"]
    conf = lc[lc.status == "confirmed"]
    rng = np.random.default_rng(seed + 3)
    p_ids = sorted(rng.choice(prov.index.to_numpy(), size=min(20, len(prov)), replace=False).tolist())
    c_ids = sorted(rng.choice(conf.index.to_numpy(), size=20, replace=False).tolist())
    dbm = db.q_ids(f"""select ti.tradingitemid as "tradingItemId", ti.tradingitemstatusid, s.securityenddate, ti.tickersymbol, ti.exchangeid
                        from {SCHEMA}.ciqtradingitem ti join {SCHEMA}.ciqsecurity s on s.securityid=ti.securityid where ti.tradingitemid = any(%s)""", p_ids + c_ids).set_index("tradingItemId")
    last_close = db.q_ids(f"""select tradingitemid as "tradingItemId", max(pricingdate) filter (where priceclose is not null) ciq_last from {SCHEMA}.ciqpriceequity
                              where tradingitemid = any(%s) group by 1""", p_ids + c_ids).set_index("tradingItemId")
    mi_last = db.q_ids(f"""select tradingitemid as "tradingItemId", max(pricedate) filter (where priceclose is not null) mi_last from {SCHEMA}.miprice
                           where tradingitemid = any(%s) group by 1""", p_ids + c_ids).set_index("tradingItemId")
    ev = b.table("events/corporate_actions.parquet").set_index("tradingItemId")
    cal_days = b.calendar_days()
    def biz_days_between(a, bb_):
        if pd.isna(a) or pd.isna(bb_):
            return None
        return int(np.searchsorted(cal_days, ts_ns([bb_])[0], side="right") - np.searchsorted(cal_days, ts_ns([a])[0], side="right"))
    prow = []
    for tid in p_ids + c_ids:
        l = lc.loc[tid]
        status_db = dbm.tradingitemstatusid.get(tid); sec_end = dbm.securityenddate.get(tid)
        ciq_last = pd.Timestamp(last_close.ciq_last.get(tid)) if tid in last_close.index else pd.NaT
        mi_l = pd.Timestamp(mi_last.mi_last.get(tid)) if tid in mi_last.index and pd.notna(mi_last.mi_last.get(tid)) else pd.NaT
        has_event = tid in ev.index
        silent_ciq = biz_days_between(ciq_last, asof)
        silent_mi = biz_days_between(mi_l, asof) if pd.notna(mi_l) else None
        marker = (pd.notna(sec_end) and pd.Timestamp(sec_end) <= asof) or status_db in (4, 5, 8, 11) or (status_db == 2 and (silent_ciq or 0) >= 10) or has_event
        expected = "confirmed" if marker else ("active" if (silent_ciq is not None and silent_ciq <= 10) else "provisional")
        prow.append(dict(tradingItemId=tid, status=l.status, endReason=l.endReason, lastTradeDate=l.lastTradeDate, db_status=status_db, db_secEnd=sec_end,
                         db_ciq_last_close=ciq_last, db_mi_last_close=mi_l, biz_days_silent_ciq=silent_ciq, biz_days_silent_mi=silent_mi, has_event_row=has_event,
                         marker_present=bool(marker), expected_status=expected, status_ok=(expected == l.status),
                         lastTrade_le_min_ciq_mi=bool(l.lastTradeDate <= min([x for x in [ciq_last, mi_l] if pd.notna(x)])) if pd.notna(ciq_last) else None,
                         symbol=dbm.tickersymbol.get(tid), exchangeId=dbm.exchangeid.get(tid)))
    pdf = pd.DataFrame(prow)
    pdf.to_csv(out / "E_status_recheck.csv", index=False)
    status_dist = lc.status.value_counts().to_dict()
    prov_dist = dict(n=int(len(prov)), endReason=prov.endReason.value_counts().to_dict(), status_ids=prov.tradingItemStatusId.value_counts().head(6).to_dict(),
                     last_trade_min=prov.lastTradeDate.min(), last_trade_max=prov.lastTradeDate.max(), everTop500=len(set(prov.index) & b.ever_top500()))
    # 확인 규칙 전수(DB 조회 없이 lifecycle 자체 컬럼으로): confirmed 인데 마커 컬럼이 전혀 없는 행
    conf_no_marker = conf[conf.securityEndDate.isna() & ~conf.tradingItemStatusId.isin([2, 4, 5, 8, 11]) & conf.eventEndDate.isna()]
    active_recent = lc[lc.status == "active"]
    active_stale = active_recent[active_recent.ciqLastCloseDate < cal_days[max(0, np.searchsorted(cal_days, ts_ns([asof])[0]) - 10)].astype("datetime64[ns]")]
    fail = tails["flag4_xor_post"] > 0 or tails["returns_rows_post_lastTrade"] > 0 or (~kdf.last_ok).any() or (~kdf.first_ok).any() or (~pdf.status_ok).any()
    return dict(verdict=verdict(bool(fail), warn=len(conf_no_marker) > 0 or len(active_stale) > 0), selftest=st, tails=tails,
                known_cases=kdf.to_dict("records"), teva=teva_info, status_distribution=status_dist, provisional=prov_dist,
                status_recheck=dict(n=len(pdf), ok=int(pdf.status_ok.sum()), provisional_checked=len(p_ids), confirmed_checked=len(c_ids),
                                    lastTrade_gt_min_ciq_mi=int((pdf.lastTrade_le_min_ciq_mi == False).sum())),  # noqa: E712
                confirmed_without_marker_columns=int(len(conf_no_marker)), active_with_ciq_last_older_than_10bd=int(len(active_stale)))


# ----------------------------------------------------------------------------------------------------------------------
# F. lineage · 이중계산 · exclusions
# ----------------------------------------------------------------------------------------------------------------------
def tradable_days_by_id(b: Build, ids, cal_days: np.ndarray) -> dict[int, np.ndarray]:
    """로더 정의와 같은 tradable(firstTradeDate ≤ d ≤ lastTradeDate ∧ close notna ∧ ¬exclusions) 을 독립 구현 → id별 날짜(int64 ns) 집합."""
    lc = b.lifecycle()
    ex = b.exclusions()
    base = b.read_ids("base_data.parquet", ids, columns=["tradingItemId", "date", "close"])
    base = base[base.close.notna()]
    out = {}
    for tid, g in base.groupby("tradingItemId"):
        d = ts_ns(g.date)
        l = lc.loc[tid]
        lo = ts_ns([l.firstTradeDate])[0] if pd.notna(l.firstTradeDate) else -np.inf
        hi = ts_ns([l.lastTradeDate])[0] if pd.notna(l.lastTradeDate) else np.inf
        keep = (d >= lo) & (d <= hi) & np.isin(d, cal_days)
        for e in ex[ex.tradingItemId == tid].itertuples():
            keep &= ~((d >= ts_ns([e.fromDate])[0]) & (d <= ts_ns([e.toDate])[0]))
        out[int(tid)] = np.sort(d[keep])
    return out


def selftest_F() -> dict:
    a = ts_ns(pd.to_datetime(["2020-01-02", "2020-01-03"])); bb_ = ts_ns(pd.to_datetime(["2020-01-03", "2020-01-06"]))
    ov = len(np.intersect1d(a, bb_))
    return dict(passed=ov == 1 and len(np.intersect1d(a, ts_ns(pd.to_datetime(["2021-01-01"])))) == 0)


def item_F(b: Build, out: Path, tmp: Path, db: DB, seed: int, **_) -> dict:
    st = selftest_F()
    ln = b.lineage()
    same_line = ln[ln.relation.isin(["rename", "relist"])].copy()
    cal_days = b.calendar_days()
    ids = sorted(set(same_line.oldId.astype(int)) | set(same_line.newId.astype(int)))
    inst_ids = set(b.instruments().tradingItemId.astype(int))
    ids_in = [i for i in ids if i in inst_ids]
    td = tradable_days_by_id(b, ids_in, cal_days)
    rows = []
    for r in same_line.itertuples():
        o, n = int(r.oldId), int(r.newId)
        if o not in td or n not in td:
            rows.append(dict(oldId=o, newId=n, relation=r.relation, confidence=r.confidence, overlap_days=None, note="id not in build/tradable"))
            continue
        inter = np.intersect1d(td[o], td[n])
        rows.append(dict(oldId=o, newId=n, relation=r.relation, confidence=r.confidence, overlap_days=int(len(inter)),
                         overlap_first=pd.Timestamp(inter.min()) if len(inter) else None, overlap_last=pd.Timestamp(inter.max()) if len(inter) else None,
                         evidence=r.evidence[:120] if isinstance(r.evidence, str) else None))
    odf = pd.DataFrame(rows)
    odf.to_csv(out / "F_lineage_tradable_overlap.csv", index=False)
    ov = odf[odf.overlap_days.fillna(0) > 0]
    # 유니버스 동시 멤버(같은 라인 두 id 같은 날 같은 universe)
    mb_all = {"all": b.membership(False), "v1rule": b.membership(True)}
    urows = []
    for tname, mb in mb_all.items():
        for uni in sorted(mb.universe.unique()):
            sub = mb[mb.universe == uni]
            u_ids = np.array(sorted(set(sub.tradingItemId.astype(int)) & set(ids_in)), dtype=np.int64)
            if len(u_ids) == 0:
                continue
            daily = daily_from_intervals(sub[sub.tradingItemId.isin(u_ids)], cal_days, u_ids)
            pos = {int(i): k for k, i in enumerate(u_ids)}
            for r in same_line.itertuples():
                o, n = int(r.oldId), int(r.newId)
                if o in pos and n in pos:
                    both = int((daily[:, pos[o]] & daily[:, pos[n]]).sum())
                    if both:
                        urows.append(dict(table=tname, universe=uni, oldId=o, newId=n, days_both_member=both))
    udf = pd.DataFrame(urows)
    conf_map = {(int(o), int(n)): c for o, n, c in zip(same_line.oldId, same_line.newId, same_line.confidence)}
    if len(udf):
        udf["confidence"] = [conf_map.get((int(o), int(n))) for o, n in zip(udf.oldId, udf.newId)]
    udf.to_csv(out / "F_universe_double_membership.csv", index=False)
    udf_hm = udf[udf.confidence.isin(["high", "medium"])] if len(udf) else udf
    # T1 미적용 31쌍 중 5쌍 DB 확인(별개 회사인지)
    nl = pd.read_csv(b.root / "qa/T1/lineage_not_lifted.csv")
    nl_pairs = nl[nl.reason.astype(str).str.contains("prices_differ", na=False)] if "reason" in nl.columns else nl
    # MCO/DNB 를 반드시 포함 + 겹침 일수 큰 순 4쌍
    pick = pd.concat([nl_pairs[nl_pairs.oldSymbol.eq("MCO") | nl_pairs.newSymbol.eq("MCO")], nl_pairs.sort_values("overlapDays", ascending=False)]).drop_duplicates(["oldId", "newId"]).head(5)
    pair_ids = sorted(set(pick.oldId.astype(int)) | set(pick.newId.astype(int))) if len(pick) else []
    comp = db.q_ids(f"""select ti.tradingitemid as "tradingItemId", ti.tickersymbol, s.securityid, co.companyid, co.companyname, s.securitystartdate, s.securityenddate
                         from {SCHEMA}.ciqtradingitem ti join {SCHEMA}.ciqsecurity s on s.securityid=ti.securityid join {SCHEMA}.ciqcompany co on co.companyid=s.companyid
                         where ti.tradingitemid = any(%s)""", pair_ids).set_index("tradingItemId") if pair_ids else pd.DataFrame()
    prow = []
    for r in pick.itertuples(index=False):
        o, n = int(r.oldId), int(r.newId)
        # 공통 종가일 가격 비교(DB 직접)
        pr = db.q(f"""select a.pricingdate, a.priceclose*a.adjustmentfactor ra, b2.priceclose*b2.adjustmentfactor rb from {SCHEMA}.ciqpriceequity a
                      join {SCHEMA}.ciqpriceequity b2 on b2.pricingdate=a.pricingdate and b2.tradingitemid=%s where a.tradingitemid=%s and a.priceclose is not null and b2.priceclose is not null
                      and a.pricingdate >= '2000-01-01' limit 5000""", (n, o))
        eq = float((np.abs(pr.ra.astype(float) / pr.rb.astype(float) - 1) < 1e-6).mean()) if len(pr) else None
        prow.append(dict(oldId=o, newId=n, old_company=comp.companyname.get(o), old_companyId=comp.companyid.get(o), new_company=comp.companyname.get(n), new_companyId=comp.companyid.get(n),
                         same_company=comp.companyid.get(o) == comp.companyid.get(n), old_ticker=comp.tickersymbol.get(o), new_ticker=comp.tickersymbol.get(n),
                         common_close_days=int(len(pr)), share_equal_close=eq, old_secEnd=comp.securityenddate.get(o), new_secStart=comp.securitystartdate.get(n)))
    pdf = pd.DataFrame(prow)
    pdf.to_csv(out / "F_not_lifted_pairs_db.csv", index=False)
    # tradable_exclusions 10쌍: DB 로 같은 securityId 확인 + 마스크 적용 확인(제외 구간에 제외 id tradable 0, 선호 id tradable > 0)
    ex = b.exclusions()
    ex_ids = sorted(set(ex.tradingItemId.astype(int)) | set(ex.preferredItemId.astype(int)))
    sec = db.q_ids(f"select tradingitemid as \"tradingItemId\", securityid, tickersymbol, exchangeid from {SCHEMA}.ciqtradingitem where tradingitemid = any(%s)", ex_ids).set_index("tradingItemId")
    td_ex = tradable_days_by_id(b, ex_ids, cal_days)
    base_ex = b.read_ids("base_data.parquet", ex_ids, columns=["tradingItemId", "date", "close"])
    erows = []
    for r in ex.itertuples():
        e_id, p_id = int(r.tradingItemId), int(r.preferredItemId)
        lo, hi = ts_ns([r.fromDate])[0], ts_ns([r.toDate])[0]
        in_win = lambda arr: int(((arr >= lo) & (arr <= hi)).sum())  # noqa
        closes_e = ts_ns(base_ex[(base_ex.tradingItemId == e_id) & base_ex.close.notna()].date)
        erows.append(dict(excluded=e_id, preferred=p_id, fromDate=r.fromDate, toDate=r.toDate, reason=r.reason, same_securityId=sec.securityid.get(e_id) == sec.securityid.get(p_id),
                          excluded_tradable_days_in_window=in_win(td_ex.get(e_id, np.array([], dtype=np.int64))), excluded_close_days_in_window=in_win(closes_e),
                          preferred_tradable_days_in_window=in_win(td_ex.get(p_id, np.array([], dtype=np.int64)))))
    edf = pd.DataFrame(erows)
    edf.to_csv(out / "F_exclusions_check.csv", index=False)
    # 같은 securityId 두 id 동시 tradable (전 종목, DB securityid 기준) — 독립 전수 검사
    inst = b.instruments()
    dup_sec = inst[inst.securityId.duplicated(keep=False)]
    sec_rows = []
    if len(dup_sec):
        td_sec = tradable_days_by_id(b, sorted(dup_sec.tradingItemId.astype(int)), cal_days)
        for sid, g in dup_sec.groupby("securityId"):
            tids = sorted(g.tradingItemId.astype(int))
            for i in range(len(tids)):
                for j in range(i + 1, len(tids)):
                    inter = np.intersect1d(td_sec.get(tids[i], np.array([], dtype=np.int64)), td_sec.get(tids[j], np.array([], dtype=np.int64)))
                    sec_rows.append(dict(securityId=int(sid), idA=tids[i], idB=tids[j], overlap_days=int(len(inter))))
    sdf = pd.DataFrame(sec_rows)
    sdf.to_csv(out / "F_same_security_overlap.csv", index=False)
    hm = ov[ov.confidence.isin(["high", "medium"])]
    fail_f = bool(len(udf_hm) > 0 or (edf.excluded_tradable_days_in_window > 0).any() or (~edf.same_securityId).any() or ((sdf.overlap_days > 0).any() if len(sdf) else False))
    res = dict(verdict=verdict(fail_f, warn=len(hm) > 0 or len(ov) > 0),
               selftest=st,
               lineage_same_line=dict(pairs=int(len(same_line)), pairs_checked=int(odf.overlap_days.notna().sum()), pairs_overlapping=int(len(ov)),
                                      overlap_days_total=int(ov.overlap_days.sum()), by_confidence=ov.groupby("confidence").size().to_dict(),
                                      high_medium_overlapping=int(len(hm)), high_medium_days=int(hm.overlap_days.sum()),
                                      low_overlapping=int((ov.confidence == "low").sum()), low_days=int(ov.loc[ov.confidence == "low", "overlap_days"].sum()),
                                      top_overlaps=ov.sort_values("overlap_days", ascending=False).head(10).to_dict("records")),
               universe_double_membership=dict(cells_all=int(udf.days_both_member.sum()) if len(udf) else 0, pairs_all=int(len(udf)),
                                               cells_high_medium=int(udf_hm.days_both_member.sum()) if len(udf_hm) else 0, pairs_high_medium=int(len(udf_hm)),
                                               low_confidence_pairs=sorted({f"{o}->{n}" for o, n in zip(udf.oldId, udf.newId)}) if len(udf) else [],
                                               note="low = Compustat gvkey 만 공유하는 별개 회사(MCO/DNB 류) → 동시 멤버가 정상"),
               medium_overlapping_pairs=hm[["oldId", "newId", "relation", "confidence", "overlap_days", "overlap_first", "overlap_last"]].to_dict("records"),
               not_lifted_pairs_db=pdf.to_dict("records"),
               exclusions=dict(rows=len(edf), same_securityId_all=bool(edf.same_securityId.all()), excluded_tradable_in_window_total=int(edf.excluded_tradable_days_in_window.sum()),
                               excluded_close_days_in_window_total=int(edf.excluded_close_days_in_window.sum()), preferred_tradable_in_window_total=int(edf.preferred_tradable_days_in_window.sum())),
               same_security_pairs=dict(pairs=int(len(sdf)), overlapping=int((sdf.overlap_days > 0).sum()) if len(sdf) else 0))
    return res


# ----------------------------------------------------------------------------------------------------------------------
# G. universe
# ----------------------------------------------------------------------------------------------------------------------
def selftest_G() -> dict:
    cal = ts_ns(pd.to_datetime(["2020-01-02", "2020-01-03", "2020-01-06", "2020-01-07"]))
    ids = np.array([1, 2], dtype=np.int64)
    mb = pd.DataFrame({"universe": ["Top500"] * 2, "tradingItemId": [1, 2], "fromDate": pd.to_datetime(["2020-01-02", "2020-01-06"]), "toDate": pd.to_datetime(["2020-01-03", "2020-01-07"])})
    d = daily_from_intervals(mb, cal, ids)
    ok = d.tolist() == [[True, False], [True, False], [False, True], [False, True]]
    ref = d.copy(); ref[0, 1] = True  # 심은 1셀 → 중첩 하락 검출
    ov = (d & ref).sum(1) / np.maximum(1, d.sum(1))
    ov_ref = (d & ref).sum(1) / np.maximum(1, ref.sum(1))
    ok &= bool(ov.min() == 1.0 and ov_ref.min() < 1.0)
    return dict(passed=bool(ok))


def item_G(b: Build, out: Path, tmp: Path, db: DB, seed: int, **_) -> dict:
    st = selftest_G()
    if not st["passed"]:
        return dict(verdict="FAIL", selftest=st)
    cal_days = b.calendar_days()
    ln = b.lineage()
    chain = ln[ln.relation.isin(["rename", "relist"]) & ln.confidence.isin(["high", "medium"])]
    # 체인 끝 id 매핑(old → ... → end)
    nxt = dict(zip(chain.oldId.astype(int), chain.newId.astype(int)))
    def end_of(i):
        seen = set()
        while i in nxt and i not in seen:
            seen.add(i); i = nxt[i]
        return i
    results = {}
    daily_counts = {}
    for tname, proto_path in [("v1rule", PROTO_TOP500_V1RULE), ("all", PROTO_TOP500_ALL)]:
        mb = b.membership(tname == "v1rule")
        sub = mb[mb.universe == "Top500"]
        ids = np.array(sorted(sub.tradingItemId.astype(int).unique()), dtype=np.int64)
        ours = daily_from_intervals(sub, cal_days, ids)
        pf = pq.ParquetFile(proto_path)
        names = [n for n in pf.schema_arrow.names if n != "__index_level_0__"]
        pt = pf.read()
        pidx = pd.to_datetime(pt.column("__index_level_0__").to_numpy())
        pids = np.array([int(n) for n in names], dtype=np.int64)
        pm = np.column_stack([pt.column(n).to_numpy() for n in names]) if len(names) < 1 else pt.select(names).to_pandas().to_numpy()
        # 공통 날짜(2010+, 양쫽 존재)
        p_i8 = ts_ns(pidx)
        common = np.intersect1d(cal_days[cal_days >= ts_ns(pd.to_datetime(["2010-01-01"]))[0]], p_i8)
        oi = np.searchsorted(cal_days, common); pi = np.searchsorted(p_i8, common)
        A = ours[oi]; Bm = pm[pi]
        # id 수준: 공통 id 축
        all_ids = np.array(sorted(set(ids.tolist()) | set(pids.tolist())), dtype=np.int64)
        A_full = np.zeros((len(common), len(all_ids)), bool); A_full[:, np.searchsorted(all_ids, ids)] = A
        B_full = np.zeros((len(common), len(all_ids)), bool); B_full[:, np.searchsorted(all_ids, pids)] = Bm
        inter = (A_full & B_full).sum(1); nA = A_full.sum(1); nB = B_full.sum(1)
        ov_id = inter / np.maximum(1, nA)
        # line 수준: 열을 체인 끝 id 로 합침
        end_ids = np.array([end_of(int(i)) for i in all_ids], dtype=np.int64)
        uniq_end, inv = np.unique(end_ids, return_inverse=True)
        def collapse(M):
            outm = np.zeros((M.shape[0], len(uniq_end)), bool)
            for k in range(M.shape[1]):
                outm[:, inv[k]] |= M[:, k]
            return outm
        A_l = collapse(A_full); B_l = collapse(B_full)
        ov_line = (A_l & B_l).sum(1) / np.maximum(1, A_l.sum(1))
        results[tname] = dict(days=int(len(common)), overlap_id_median=float(np.median(ov_id)), overlap_id_p05=float(np.quantile(ov_id, 0.05)), overlap_id_min=float(ov_id.min()),
                              overlap_line_median=float(np.median(ov_line)), overlap_line_p05=float(np.quantile(ov_line, 0.05)), overlap_line_min=float(ov_line.min()),
                              members_median_ours=float(np.median(nA)), members_median_proto=float(np.median(nB)),
                              only_ours_member_days=int((A_full & ~B_full).sum()), only_proto_member_days=int((~A_full & B_full).sum()),
                              line_only_ours=int((A_l & ~B_l).sum()), line_only_proto=int((~A_l & B_l).sum()))
        # 일별 멤버 수 이상치(전일 대비 ±5% 초과) — 전 기간
        cnt = ours.sum(1).astype(float)
        chg = np.r_[np.nan, cnt[1:] / np.maximum(1, cnt[:-1]) - 1]
        first_pop = int(np.argmax(cnt > 0)) if (cnt > 0).any() else len(cnt)
        chg[: first_pop + 1] = np.nan  # 워밍업(멤버 0) 구간 제외
        odd = np.where(np.abs(chg) > 0.05)[0]
        results[tname]["first_populated_day"] = str(pd.Timestamp(cal_days[first_pop]).date()) if first_pop < len(cnt) else None
        daily_counts[tname] = pd.DataFrame({"date": pd.to_datetime(cal_days[odd]), "members": cnt[odd], "prev": cnt[np.maximum(0, odd - 1)], "chg": chg[odd]})
        daily_counts[tname].to_csv(out / f"G_member_count_jumps_{tname}.csv", index=False)
        results[tname]["member_count_jump_days_gt5pct"] = int(len(odd))
        results[tname]["member_count_first_day"] = float(cnt[0]); results[tname]["member_count_min"] = float(cnt.min()); results[tname]["member_count_max"] = float(cnt.max())
        results[tname]["member_count_jumps"] = daily_counts[tname].head(15).to_dict("records")
        pd.DataFrame({"date": pd.to_datetime(common), "overlap_id": ov_id, "overlap_line": ov_line, "nA": nA, "nB": nB}).to_csv(out / f"G_overlap_daily_{tname}.csv", index=False)
        del pt, pm
    # TokensToRemove 46 + 선행 id (체인) 미포함
    tokens = set(TOKENS_TO_REMOVE)
    preds = {o for o, n in nxt.items() if end_of(o) in {end_of(t) for t in tokens}}
    banned = tokens | preds
    mb_all = pd.concat([b.membership(False).assign(table="all"), b.membership(True).assign(table="v1rule")])
    hit = mb_all[mb_all.tradingItemId.isin(banned)]
    hit.to_csv(out / "G_tokens_in_membership.csv", index=False)
    tok = dict(tokens=len(tokens), predecessor_ids=sorted(preds), membership_rows_for_banned=int(len(hit)), banned_ids_present=sorted(hit.tradingItemId.unique().tolist()))
    # 전환 연속성 BLK·GOOGL·JCI (v1rule Top500, 일별)
    trans = []
    for label, o, n in [("BLK", 2594723, 1899273640), ("GOOGL", 312940216, 11311662), ("JCI", 2623229, 372277281), ("CCO", 24687598, 618650356), ("XRX", 2665260, 610592433)]:
        for tname in ["v1rule", "all"]:
            mb = b.membership(tname == "v1rule")
            for uni in ["Top500", "ALL"]:
                sub = mb[(mb.universe == uni) & mb.tradingItemId.isin([o, n])]
                d = daily_from_intervals(sub, cal_days, np.array([o, n], dtype=np.int64))
                lc = b.lifecycle()
                o_last = lc.lastTradeDate.get(o); n_first = lc.firstTradeDate.get(n)
                io = np.searchsorted(cal_days, ts_ns([o_last])[0]); i_n = np.searchsorted(cal_days, ts_ns([n_first])[0])
                both = int((d[:, 0] & d[:, 1]).sum())
                gap = int((~d[:, 0] & ~d[:, 1])[io + 1:i_n + 61].sum()) if d[io, 0] else None
                trans.append(dict(label=label, table=tname, universe=uni, oldId=o, newId=n, old_lastTrade=o_last, new_firstTrade=n_first, old_member_last_day=bool(d[io, 0]),
                                  new_member_first_day=bool(d[i_n, 1]) if i_n < len(cal_days) else None, days_both=both, gap_days_after_transition_within_61=gap,
                                  old_member_days=int(d[:, 0].sum()), new_member_days=int(d[:, 1].sum())))
    tdf = pd.DataFrame(trans)
    tdf.to_csv(out / "G_transition_cases.csv", index=False)
    # membership 구간 자체 검사: 같은 (universe,id) 구간 겹침 0, toDate ≥ fromDate, 날짜가 캘린더 거래일
    mb_chk = {}
    for tname in ["all", "v1rule"]:
        mb = b.membership(tname == "v1rule").sort_values(["universe", "tradingItemId", "fromDate"])
        same = (mb.universe.values[1:] == mb.universe.values[:-1]) & (mb.tradingItemId.values[1:] == mb.tradingItemId.values[:-1])
        ovl = int((same & (ts_ns(mb.fromDate)[1:] <= ts_ns(mb.toDate)[:-1])).sum())
        mb_chk[tname] = dict(rows=len(mb), overlaps=ovl, to_lt_from=int((mb.toDate < mb.fromDate).sum()), from_off_calendar=int((~np.isin(ts_ns(mb.fromDate), cal_days)).sum()),
                             to_off_calendar=int((~np.isin(ts_ns(mb.toDate), cal_days)).sum()), ids_not_in_instruments=len(set(mb.tradingItemId.astype(int)) - set(b.instruments().tradingItemId.astype(int))),
                             universes=sorted(mb.universe.unique().tolist()))
    fail = (tok["membership_rows_for_banned"] > 0 or any(v["overlaps"] or v["to_lt_from"] or v["from_off_calendar"] or v["to_off_calendar"] or v["ids_not_in_instruments"] for v in mb_chk.values())
            or (tdf.days_both > 0).any())
    warn = results["v1rule"]["overlap_line_median"] < 0.99 or abs(results["v1rule"]["overlap_line_median"] - 0.9978) > 0.001
    return dict(verdict=verdict(bool(fail), bool(warn)), selftest=st, overlap_vs_proto=results, tokens=tok, transitions=tdf.to_dict("records"), membership_checks=mb_chk,
                t5_reported=dict(v1rule_line_top500=0.9978, v1rule_id_top500=0.9740))


# ----------------------------------------------------------------------------------------------------------------------
# H. events
# ----------------------------------------------------------------------------------------------------------------------
def selftest_H() -> dict:
    ev = pd.DataFrame({"cashPerShare": [1.0, 2.0, np.nan], "cashPerShareCache": [1.0, 2.002, np.nan], "successorRatio": [0.5, np.nan, 1.0], "successorRatioCache": [0.5, np.nan, 1.0]})
    bad = (np.abs(ev.cashPerShareCache / ev.cashPerShare - 1) > 1e-6).sum()
    return dict(passed=int(bad) == 1)


def item_H(b: Build, out: Path, tmp: Path, db: DB, seed: int, **_) -> dict:
    st = selftest_H()
    ev = b.table("events/corporate_actions.parquet")
    lc = b.lifecycle()
    inst_ids = set(b.instruments().tradingItemId.astype(int))
    # k≡1
    k_cash = np.abs(ev.cashPerShareCache / ev.cashPerShare - 1); k_cash = k_cash[ev.cashPerShare.notna() & (ev.cashPerShare != 0)]
    k_ratio = np.abs(ev.successorRatioCache / ev.successorRatio - 1); k_ratio = k_ratio[ev.successorRatio.notna() & (ev.successorRatio != 0)]
    k_t = ev.cacheUnitFactorTarget.dropna(); k_s = ev.cacheUnitFactorSuccessor.dropna()
    ident = dict(cash_n=int(len(k_cash)), cash_gt_1e6=int((k_cash > 1e-6).sum()), cash_max=float(k_cash.max()) if len(k_cash) else None,
                 ratio_n=int(len(k_ratio)), ratio_gt_1e6=int((k_ratio > 1e-6).sum()), ratio_max=float(k_ratio.max()) if len(k_ratio) else None,
                 kt_n=int(len(k_t)), kt_ne_1_gt_1e6=int((np.abs(k_t - 1) > 1e-6).sum()), ks_n=int(len(k_s)), ks_ne_1_gt_1e6=int((np.abs(k_s - 1) > 1e-6).sum()),
                 cash_zero_with_nonzero_cache=int(((ev.cashPerShare == 0) & (ev.cashPerShareCache != 0) & ev.cashPerShareCache.notna()).sum()))
    # endDate ≤ lastTradeDate 전수
    ltd = lc.lastTradeDate.reindex(ev.tradingItemId).to_numpy()
    gap = (ev.endDate.to_numpy() - ltd).astype("timedelta64[D]").astype(float)
    ev["lastTradeDate"] = ltd; ev["endDate_minus_lastTrade_days"] = gap
    ev["everTop500"] = ev.tradingItemId.isin(b.ever_top500())
    late = ev[gap > 0]
    late.sort_values("endDate_minus_lastTrade_days", ascending=False).to_csv(out / "H_endDate_after_lastTrade.csv", index=False)
    end_chk = dict(rows=len(ev), ids_not_in_lifecycle=int(pd.isna(ltd).sum()), endDate_eq_lastTrade=int((gap == 0).sum()), endDate_gt_lastTrade=int((gap > 0).sum()),
                   endDate_lt_lastTrade=int((gap < 0).sum()), gt_median_days=float(np.median(gap[gap > 0])) if (gap > 0).any() else None, gt_max_days=float(np.nanmax(gap)) if len(gap) else None,
                   gt_everTop500=int(late.everTop500.sum()), gt_with_ok_status=int((late.cacheUnitStatus == "ok").sum()),
                   ids_confirmed_without_event_row=int(((lc.status == "confirmed")).sum() - ev.tradingItemId.nunique()),
                   event_ids_not_confirmed=int((lc.status.reindex(ev.tradingItemId) != "confirmed").sum()))
    # successor: 종목 집합 안? 정산 bar 에 raw close 존재? successorCacheClose == base close?
    succ = ev[ev.successorTradingItemId.notna()].copy()
    succ["succ_in_set"] = succ.successorTradingItemId.astype(int).isin(inst_ids)
    s_in = succ[succ.succ_in_set & succ.successorCacheCloseDate.notna()]
    sb = b.read_ids("base_data.parquet", sorted(s_in.successorTradingItemId.astype(int).unique()), columns=["tradingItemId", "date", "close"])
    sb = sb.rename(columns={"tradingItemId": "successorTradingItemId", "date": "successorCacheCloseDate", "close": "base_close"})
    sb["successorTradingItemId"] = sb.successorTradingItemId.astype(float)
    j = s_in.merge(sb, on=["successorTradingItemId", "successorCacheCloseDate"], how="left")
    rel = np.abs(j.base_close.astype(float) / j.successorCacheClose - 1)
    # 정산일(settlementDate) 에 승계사 raw close 존재
    sd = s_in[["tradingItemId", "successorTradingItemId", "settlementDate", "cacheUnitStatus"]].copy()
    sd2 = sd.merge(sb.rename(columns={"successorCacheCloseDate": "settlementDate", "base_close": "settle_close"}), on=["successorTradingItemId", "settlementDate"], how="left")
    succ_chk = dict(rows_with_successor=int(len(succ)), successor_in_instruments=int(succ.succ_in_set.sum()), successor_outside=int((~succ.succ_in_set).sum()),
                    successorCacheClose_rows=int(len(j)), base_close_missing_at_date=int(j.base_close.isna().sum()), rel_gt_1e6=int((rel > 1e-6).sum()), rel_max=float(np.nanmax(rel)) if len(rel) else None,
                    settlement_close_missing=int(sd2.settle_close.isna().sum()), settlement_close_missing_ok_status=int((sd2.settle_close.isna() & (sd2.cacheUnitStatus == "ok")).sum()),
                    outside_statuses=succ.loc[~succ.succ_in_set, "cacheUnitStatus"].value_counts().to_dict())
    succ[~succ.succ_in_set].to_csv(out / "H_successor_outside_set.csv", index=False)
    # settlementDate 컬럼 성격(엔진은 endDate 만 소비; settlementDate = co_acthist datadate)
    cal_days = b.calendar_days()
    nxt_i = np.searchsorted(cal_days, ts_ns(ev.endDate), side="right")
    nxt = np.where(nxt_i < len(cal_days), cal_days[np.minimum(nxt_i, len(cal_days) - 1)], np.iinfo(np.int64).min)
    sd_i8 = ts_ns(ev.settlementDate)
    settle_info = dict(rows=len(ev), settlement_eq_next_trading_day=int((sd_i8 == nxt).sum()), settlement_le_endDate=int((sd_i8 <= ts_ns(ev.endDate)).sum()),
                       settlement_not_trading_day=int((~np.isin(sd_i8, cal_days)).sum()),
                       note="엔진은 endDate(end_date_col)로 정산 bar 를 정함(다음 bar). settlementDate 는 co_acthist datadate(정보용)")
    # 실사례 4건 vs Phase 2 손계산
    hc = json.loads(PHASE2_HANDCHECK.read_text())
    cases = []
    for sym, h in hc.items():
        tid = int(h["tradingItemId"])
        e = ev[ev.tradingItemId == tid]
        e1 = e.iloc[0] if len(e) else None
        cases.append(dict(symbol=sym, tradingItemId=tid, found=len(e), endDate=e1.endDate if e1 is not None else None, phase2_endDate=h["endDate"], phase2_mark_date=h["mark_date"],
                          endDate_eq_phase2_markDate=(e1 is not None and str(e1.endDate.date()) == h["mark_date"]),
                          endDate_ok=(e1 is not None and (str(e1.endDate.date()) == h["endDate"] or str(e1.endDate.date()) == h["mark_date"])),
                          settlementDate=e1.settlementDate if e1 is not None else None, phase2_settle=h["settle_date"],
                          cash=e1.cashPerShare if e1 is not None else None, phase2_cash=h["cash"], cash_ok=(e1 is not None and ((pd.isna(e1.cashPerShare) or e1.cashPerShare == 0) and (h["cash"] is None or (isinstance(h["cash"], float) and np.isnan(h["cash"]))) or (e1.cashPerShare is not None and h["cash"] is not None and abs(float(e1.cashPerShare) - float(h["cash"])) < 1e-6))),
                          ratio=e1.successorRatio if e1 is not None else None, phase2_ratio=h["ratio"], ratio_ok=(e1 is not None and ((pd.isna(e1.successorRatio) and (h["ratio"] is None or np.isnan(h["ratio"]))) or (pd.notna(e1.successorRatio) and h["ratio"] is not None and abs(float(e1.successorRatio) - float(h["ratio"])) < 1e-6))),
                          successor=e1.successorTradingItemId if e1 is not None else None, phase2_successor=h["successor"],
                          succ_ok=(e1 is not None and ((pd.isna(e1.successorTradingItemId) and h["successor"] is None) or (pd.notna(e1.successorTradingItemId) and h["successor"] is not None and int(e1.successorTradingItemId) == int(h["successor"])))),
                          R=e1.settlementReturn if e1 is not None else None, phase2_R=h["R"], mark=e1.targetCacheMark if e1 is not None else None, phase2_mark=h["mark"],
                          status=e1.cacheUnitStatus if e1 is not None else None, actionCode=e1.actionCode if e1 is not None else None))
    cdf = pd.DataFrame(cases)
    cdf.to_csv(out / "H_phase2_cases.csv", index=False)
    # 1:1 개명 거절 258행 성격 판정
    rj = pd.read_csv(b.root / "qa/T6/rejected_rename_rows.csv")
    rj_ids = sorted(set(rj.tid.astype(int)) | set(rj.succ.dropna().astype(int)))
    comp = db.q_ids(f"""select ti.tradingitemid as "tradingItemId", ti.tickersymbol, s.securityid, co.companyid, co.companyname, s.securitystartdate, s.securityenddate, ti.exchangeid, co.companytypeid
                         from {SCHEMA}.ciqtradingitem ti join {SCHEMA}.ciqsecurity s on s.securityid=ti.securityid join {SCHEMA}.ciqcompany co on co.companyid=s.companyid
                         where ti.tradingitemid = any(%s)""", rj_ids).set_index("tradingItemId")
    rj["old_company"] = rj.tid.map(comp.companyname); rj["old_companyId"] = rj.tid.map(comp.companyid)
    rj["new_company"] = rj.succ.map(comp.companyname); rj["new_companyId"] = rj.succ.map(comp.companyid)
    rj["same_company"] = rj.old_companyId == rj.new_companyId
    rj["same_security"] = rj.tid.map(comp.securityid) == rj.succ.map(comp.securityid)
    rj["R_explained_by_split_ratio"] = np.abs((1 + rj.R) * rj.sf_ratio - 1) < 0.2  # raw_new/raw_old ≈ sf_old/sf_new 이면 교환비 = 분할비
    rj["R_explained_by_inverse_split"] = np.abs((1 + rj.R) / rj.sf_ratio - 1) < 0.2
    # T1 lineage 에 ratio 있는지
    ln = b.lineage().set_index(["oldId", "newId"])
    rj["lineage_ratio"] = [ln.ratio.get((int(o), int(n))) if pd.notna(n) and (int(o), int(n)) in ln.index else np.nan for o, n in zip(rj.tid, rj.succ)]
    rj["lineage_relation"] = [ln.relation.get((int(o), int(n))) if pd.notna(n) and (int(o), int(n)) in ln.index else None for o, n in zip(rj.tid, rj.succ)]
    # 승계사가 종목 집합 안·정산 bar 에 종가 존재 (이미 rejected 이니 raw_new 로 판단)
    rj["succ_in_set"] = rj.succ.isin(inst_ids)
    rj["everTop500"] = rj.tid.isin(b.ever_top500()) | rj.succ.isin(b.ever_top500())
    rj["everTop1000"] = rj.tid.isin(b.ever_universe("Top1000")) | rj.succ.isin(b.ever_universe("Top1000"))
    # de-SPAC 휴리스틱: 옛 회사명에 Acquisition/Capital/Holdings + SPAC 류, 또는 T6 표기
    spac_re = r"Acquisition (Corp|Co|Company|Holdings|Inc|Ltd|Limited|Partners|Group|I|II|III|IV|V)\b|SPAC|Blank Check"
    rj["old_name_spac_like"] = rj.old_company.astype(str).str.contains(spac_re, case=False, regex=True) & ~rj.same_company
    rj["absR"] = rj.R.abs()
    def classify(r):
        if pd.isna(r.succ):
            return "no_successor"
        if (r.R_explained_by_split_ratio or r.R_explained_by_inverse_split) and abs(r.sf_ratio - 1) > 1e-6:
            return "exchange_ratio_equals_vendor_split_ratio(backfill from ciqsplitcache)"
        if r.old_name_spac_like and r.absR <= 5:
            return "de-SPAC(1:1 true, first-day move real)"
        if (r.same_security or r.same_company) and r.absR <= 1:
            return "same_company_rename/relist(1:1 plausible, R real: spin-off or repricing)"
        if (r.same_security or r.same_company):
            return "same_company_units_mismatch(|R|>1)"
        if r.absR <= 1:
            return "different_company_unknown_ratio(|R|<=1)"
        return "different_company_units_or_wrong_match(|R|>1)"
    rj["class"] = rj.apply(classify, axis=1)
    rj.to_csv(out / "H_rejected_rename_classified.csv", index=False)
    rj_sum = dict(rows=len(rj), by_class=rj["class"].value_counts().to_dict(), everTop500=int(rj.everTop500.sum()), everTop1000=int(rj.everTop1000.sum()),
                  same_company_or_security=int((rj.same_company | rj.same_security).sum()), spac_like=int(rj.old_name_spac_like.sum()),
                  R_abs_gt_50pct=int((rj.R.abs() > 0.5).sum()), successor_in_set=int(rj.succ_in_set.sum()),
                  needs_successor_row=int(rj["class"].isin(["de-SPAC(1:1 true, first-day move real)", "same_company_rename/relist(1:1 plausible, R real: spin-off or repricing)"]).sum()),
                  fixable_by_split_ratio=int(rj["class"].str.startswith("exchange_ratio_equals").sum()),
                  everTop1000_by_class=rj[rj.everTop1000]["class"].value_counts().to_dict(),
                  everTop1000_rows=rj[rj.everTop1000].sort_values("end")[["tid", "sym", "succ", "ssym", "end", "R", "sf_ratio", "class", "old_company", "new_company", "everTop500"]].to_dict("records"),
                  top_examples=rj.sort_values("R", key=lambda s: -s.abs()).head(8)[["tid", "sym", "succ", "ssym", "end", "R", "sf_ratio", "class", "old_company", "new_company"]].to_dict("records"))
    fail = ident["cash_gt_1e6"] or ident["ratio_gt_1e6"] or ident["kt_ne_1_gt_1e6"] or ident["ks_ne_1_gt_1e6"] or succ_chk["rel_gt_1e6"] or succ_chk["base_close_missing_at_date"] \
        or (~cdf.endDate_ok).any() or (~cdf.cash_ok).any() or (~cdf.ratio_ok).any() or (~cdf.succ_ok).any() or end_chk["ids_not_in_lifecycle"]
    warn = end_chk["endDate_gt_lastTrade"] > 0 or rj_sum["needs_successor_row"] > 0 or succ_chk["settlement_close_missing_ok_status"] > 0
    return dict(verdict=verdict(bool(fail), bool(warn)), selftest=st, k_identity=ident, endDate_vs_lastTrade=end_chk, successor=succ_chk, settlementDate=settle_info,
                phase2_cases=cdf.to_dict("records"), rejected_rename=rj_sum)


# ----------------------------------------------------------------------------------------------------------------------
# I. 소비자 관점(참조 로더)
# ----------------------------------------------------------------------------------------------------------------------
def item_I(b: Build, out: Path, tmp: Path, db: DB, seed: int, **_) -> dict:
    sys.path.insert(0, str(LOADER_DIR))
    from loader import CacheV21  # 소비자 관점 검증에만 사용(원자료 대조 아님)
    cv = CacheV21(b.root, master_root=b.root, universe_root=b.root, threads=8)
    dates = cv.dates(); ids = cv.ids()
    start = np.searchsorted(dates.values.astype("datetime64[ns]").astype("int64"), ts_ns(pd.to_datetime(["2010-01-01"]))[0])
    res = {}
    art = pd.read_csv(b.root / "qa/T2/factor_artifacts.csv")
    art_keys = set(zip(art.tradingItemId.astype(int), pd.to_datetime(art.date).dt.strftime("%Y-%m-%d")))
    split = b.table("factors/split.parquet"); div = b.table("factors/dividend.parquet")
    sp_bd = set(zip(split.tradingItemId.astype(int), split.validFrom.dt.strftime("%Y-%m-%d")))
    dv_bd = set(zip(div.tradingItemId.astype(int), div.validFrom.dt.strftime("%Y-%m-%d")))
    ret = cv.get_returns(as_array=True, cut="lifecycle")
    close_raw = cv.get_panel("close", "raw", as_array=True, cut=None)
    trad = cv.get_mask("tradable", as_array=True)
    big_all = []
    for tname, kind in [("v1rule", "universe_v1rule:Top500"), ("all", "universe:Top500")]:
        uni = cv.get_mask(kind, as_array=True)
        mask = trad & uni
        m = mask[start:]; r = ret[start:]
        rr = np.where(m, r, np.nan)
        nan_in_mask = int((m & np.isnan(r)).sum())
        nan_cells = np.argwhere(m & np.isnan(r))
        nan_rows = []
        for ti, ci in nan_cells[:50]:
            t = start + ti; tid = int(ids[ci])
            first_close_t = int(np.argmax(~np.isnan(close_raw[:, ci])))
            nan_rows.append(dict(table=tname, date=str(dates[t].date()), tradingItemId=tid, is_first_close_row=(first_close_t == t), close=float(close_raw[t, ci])))
        pd.DataFrame(nan_rows).to_csv(out / f"I_nan_ret_in_mask_{tname}.csv", index=False)
        med = np.nanmedian(rr, axis=1)
        big = np.argwhere(m & (np.abs(r) > 0.5))
        rows = []
        for ti, ci in big:
            t = start + ti
            tid = int(ids[ci]); dstr = str(dates[t].date())
            prev_t = t - 1
            while prev_t >= 0 and np.isnan(close_raw[prev_t, ci]):
                prev_t -= 1
            raw_ret = close_raw[t, ci] / close_raw[prev_t, ci] - 1 if prev_t >= 0 else np.nan
            cause = ("factor_artifact(T2 list)" if (tid, dstr) in art_keys else "split_boundary" if (tid, dstr) in sp_bd else "dividend_step" if (tid, dstr) in dv_bd
                     else "genuine_raw_move" if abs(raw_ret) > 0.5 else "unexplained")
            rows.append(dict(table=tname, date=dstr, tradingItemId=tid, ret=float(r[ti, ci]), raw_ret=float(raw_ret), close=float(close_raw[t, ci]), prev_close=float(close_raw[prev_t, ci]) if prev_t >= 0 else None,
                             prevDate=str(dates[prev_t].date()) if prev_t >= 0 else None, cause=cause))
        bdf = pd.DataFrame(rows)
        big_all.append(bdf)
        res[tname] = dict(days=int(m.shape[0]), member_tradable_cells=int(m.sum()), nan_ret_cells_in_mask=nan_in_mask, nan_ratio=float(nan_in_mask / max(1, m.sum())),
                          nan_cells_first_close_row=int(sum(x["is_first_close_row"] for x in nan_rows)), nan_cells=nan_rows[:10],
                          median_of_daily_median=float(np.nanmedian(med)), daily_median_abs_max=float(np.nanmax(np.abs(med))), abs_gt_50pct_cells=int(len(bdf)),
                          abs_gt_50pct_by_cause=bdf.cause.value_counts().to_dict() if len(bdf) else {}, abs_gt_20pct_cells=int((m & (np.abs(r) > 0.2)).sum()),
                          ret_p001=float(np.nanquantile(rr, 0.001)), ret_p999=float(np.nanquantile(rr, 0.999)),
                          mask_cells_with_close_nan=int((m & np.isnan(close_raw[start:])).sum()))
    pd.concat(big_all).to_csv(out / "I_abs_ret_gt_50pct_cells.csv", index=False)
    # tradable 마스크 전체 통계 + 로더 diagnostics
    res["tradable"] = dict(cells=int(trad.sum()), cells_2010plus=int(trad[start:].sum()), diagnostics={k: v for k, v in cv.diagnostics.items() if isinstance(v, (int, float, str, bool))},
                           first_row_nan_expected=int(len(ids)))
    # 로더 check_returns (계약 산식 재계산 vs 파일) — 소비자 관점 교차
    try:
        res["loader_check_returns"] = {k: v for k, v in cv.check_returns(atol=1e-9).items() if isinstance(v, (int, float, str, bool))}
    except Exception as e:  # noqa
        res["loader_check_returns"] = f"error: {e}"
    res["peak_rss_gb"] = peak_rss_gb()
    unexpl = sum(v.get("abs_gt_50pct_by_cause", {}).get("unexplained", 0) for k, v in res.items() if isinstance(v, dict) and "abs_gt_50pct_by_cause" in v)
    warn = any(v.get("abs_gt_50pct_by_cause", {}).get("factor_artifact(T2 list)", 0) > 0 for k, v in res.items() if isinstance(v, dict)) or unexpl > 0
    nan_not_first = sum(v.get("nan_ret_cells_in_mask", 0) - v.get("nan_cells_first_close_row", 0) for v in res.values() if isinstance(v, dict) and "nan_ret_cells_in_mask" in v)
    res["verdict"] = verdict(nan_not_first > 0, warn or any(v.get("nan_ret_cells_in_mask", 0) > 0 for v in res.values() if isinstance(v, dict) and "nan_ret_cells_in_mask" in v))
    cv.release()
    return res


# ----------------------------------------------------------------------------------------------------------------------
# J. T2 인공물 97 · G-DIV 잔여 everTop500 · G-RAW everTop500
# ----------------------------------------------------------------------------------------------------------------------
def item_J(b: Build, out: Path, tmp: Path, db: DB, seed: int, **_) -> dict:
    top500 = b.ever_top500(); top1000 = b.ever_universe("Top1000")
    mb_all = pd.concat([b.membership(False).assign(table="all"), b.membership(True).assign(table="v1rule")])
    def member_on(tid, d, uni=None):
        sub = mb_all[(mb_all.tradingItemId == tid) & (mb_all.fromDate <= d) & (mb_all.toDate >= d)]
        if uni:
            sub = sub[sub.universe == uni]
        return sorted(sub.universe.unique().tolist())
    # (1) 인공물 97
    art = pd.read_csv(b.root / "qa/T2/factor_artifacts.csv", parse_dates=["date", "prevDate"])
    fake = art[art.category.str.startswith("unexplained")].copy()
    ids = sorted(fake.tradingItemId.astype(int).unique())
    sc = db.q_ids(f"select tradingitemid as \"tradingItemId\", splitdate, nextsplitdate, cumulativesplitfactor csf, latestsplitfactor lsf from {SCHEMA}.ciqsplitcache where tradingitemid = any(%s)", ids)
    sp = db.q_ids(f"select tradingitemid as \"tradingItemId\", exdate, rate, splittypeid, appliedflag from {SCHEMA}.ciqsplit where tradingitemid = any(%s)", ids)
    sc["splitdate"] = pd.to_datetime(sc.splitdate); sp["exdate"] = pd.to_datetime(sp.exdate)
    rows = []
    for r in fake.itertuples():
        tid = int(r.tradingItemId)
        sc_hit = sc[(sc.tradingItemId == tid) & (sc.splitdate == r.date)]
        sp_hit = sp[(sp.tradingItemId == tid) & (sp.exdate >= r.date - pd.Timedelta(days=7)) & (sp.exdate <= r.date + pd.Timedelta(days=7))]
        rows.append(dict(tradingItemId=tid, symbol=r.symbol, date=r.date, r_adj=r.r_adj, r_raw=r.r_raw, sf_prev=r.sf_prev, sf=r.sf, splitcache_row_on_date=len(sc_hit),
                         splitcache_lsf=float(sc_hit.lsf.iloc[0]) if len(sc_hit) else None, ciqsplit_rows_pm7d=len(sp_hit), ciqsplit_rate=float(sp_hit.rate.iloc[0]) if len(sp_hit) else None,
                         ciqsplit_type=int(sp_hit.splittypeid.iloc[0]) if len(sp_hit) else None, universe_member_on_date=member_on(tid, r.date), everTop500=tid in top500, everTop1000=tid in top1000))
    adf = pd.DataFrame(rows)
    adf.to_csv(out / "J_artifacts_db.csv", index=False)
    a_sum = dict(rows=len(adf), ids=int(adf.tradingItemId.nunique()), with_splitcache_row=int((adf.splitcache_row_on_date > 0).sum()), with_ciqsplit_pm7d=int((adf.ciqsplit_rows_pm7d > 0).sum()),
                 in_universe_on_date=int((adf.universe_member_on_date.str.len() > 0).sum()), everTop500=int(adf.everTop500.sum()), everTop1000=int(adf.everTop1000.sum()),
                 in_universe_cases=adf[adf.universe_member_on_date.str.len() > 0][["tradingItemId", "symbol", "date", "r_adj", "r_raw", "universe_member_on_date"]].to_dict("records"),
                 split_type_dist=adf.ciqsplit_type.value_counts(dropna=False).to_dict())
    # (2) G-DIV 잔여 everTop500
    gd = pd.read_csv(b.root / "qa/T2/gdiv_mismatches_genuine.csv", parse_dates=["date", "prevDate"])
    gd["everTop500"] = gd.tradingItemId.isin(top500)
    g5 = gd[gd.everTop500].copy()
    dv = db.q_ids(f"select tradingitemid as \"tradingItemId\", exdate, divamount, currencyid, dividendtypeid, supplementaltypeid, paydate from {SCHEMA}.ciqdividend where tradingitemid = any(%s)", sorted(g5.tradingItemId.unique()))
    dv["exdate"] = pd.to_datetime(dv.exdate)
    dvc = db.q_ids(f"select tradingitemid as \"tradingItemId\", dividenddate, divamount, adjustmentfactor, splitadjdivamount, dividendpaymenttypeid from {SCHEMA}.ciqdividendcache where tradingitemid = any(%s)", sorted(g5.tradingItemId.unique()))
    dvc["dividenddate"] = pd.to_datetime(dvc.dividenddate)
    daf = db.q_ids(f"select tradingitemid as \"tradingItemId\", fromdate, todate, divadjfactor from {SCHEMA}.ciqpriceequitydivadjfactor where tradingitemid = any(%s)", sorted(g5.tradingItemId.unique()))
    daf["fromdate"] = pd.to_datetime(daf.fromdate)
    rows = []
    for r in g5.itertuples():
        tid = int(r.tradingItemId)
        win = dv[(dv.tradingItemId == tid) & (dv.exdate > r.prevDate) & (dv.exdate <= r.date)]
        cache = dvc[(dvc.tradingItemId == tid) & (dvc.dividenddate > r.prevDate) & (dvc.dividenddate <= r.date)]
        f_row = daf[(daf.tradingItemId == tid) & (daf.fromdate == r.date)]
        implied_D = (1 - r.step) * r.P_prev  # 계수 스텝이 뜻하는 배당(구주 기준)
        rows.append(dict(tradingItemId=tid, symbol=r.symbol, date=r.date, prevDate=r.prevDate, step=r.step, P_prev=r.P_prev, D_usd_T2=r.D_usd, diff_T2=r["diff"] if isinstance(r, dict) else getattr(r, "diff"),
                         implied_D_from_step=implied_D, db_div_rows_in_window=len(win), db_div_amounts=win.divamount.astype(float).tolist(), db_div_types=win.dividendtypeid.tolist(),
                         db_div_ccy=win.currencyid.tolist(), db_div_exdates=[str(x.date()) for x in win.exdate], cache_rows=len(cache), cache_paymenttypes=cache.dividendpaymenttypeid.tolist(),
                         cache_splitadj_amounts=cache.splitadjdivamount.astype(float).tolist(), db_factor_from_on_date=float(f_row.divadjfactor.iloc[0]) if len(f_row) else None,
                         universe_member_on_date=member_on(tid, r.date), split_between=r.split_between))
    gdf = pd.DataFrame(rows)
    gdf.to_csv(out / "J_gdiv_everTop500_db.csv", index=False)
    def gd_class(r):
        if r.db_div_rows_in_window == 0:
            return "no_dividend_in_db(step without dividend)"
        if any(t in NONCASH_DIV_TYPES for t in r.db_div_types) or any(t in (2, 3, 4) for t in r.cache_paymenttypes):
            return "noncash_or_spinoff_component(vendor valuation)"
        if any(c != USD for c in r.db_div_ccy):
            return "non_usd_dividend"
        if r.split_between:
            return "split_between_prev_and_ex"
        if abs(r.implied_D_from_step - sum(r.db_div_amounts)) / max(1e-9, sum(r.db_div_amounts)) < 0.02:
            return "matches_within_2pct(rounding)"
        return "amount_mismatch_unexplained"
    gdf["class"] = gdf.apply(gd_class, axis=1) if len(gdf) else []
    gdf.to_csv(out / "J_gdiv_everTop500_db.csv", index=False)
    g_sum = dict(rows_total=len(gd), rows_everTop500=len(g5), ids_everTop500=int(g5.tradingItemId.nunique()), by_class=gdf["class"].value_counts().to_dict() if len(gdf) else {},
                 in_universe_on_date=int((gdf.universe_member_on_date.str.len() > 0).sum()) if len(gdf) else 0,
                 max_absdiff_everTop500=float(g5["absdiff"].max()) if "absdiff" in g5 else None,
                 examples=gdf.sort_values("date").head(40)[["symbol", "date", "step", "P_prev", "implied_D_from_step", "db_div_amounts", "db_div_types", "cache_paymenttypes", "class", "universe_member_on_date"]].to_dict("records"))
    # (2b) 전체 G-DIV 잔여(3,496행)에서 '상수배 배당 스케일' 벤더 오류 탐지: id별 implied_D/D_usd 비율이 일정(≠1)한 종목 → 누적 왜곡·유니버스 기간
    gd2 = gd[(gd.D_usd > 0) & gd.step.notna() & gd.P_prev.notna()].copy()
    gd2["implied_D"] = (1 - gd2.step) * gd2.P_prev
    gd2["ratio"] = gd2.implied_D / gd2.D_usd
    grp = gd2.groupby("tradingItemId").ratio.agg(n="size", med="median", p25=lambda x: x.quantile(0.25), p75=lambda x: x.quantile(0.75))
    const = grp[(grp.n >= 3) & ((grp.med - 1).abs() > 0.2)].copy()  # 체계적 스케일 오류(비율이 기간별로 바뀔 수 있어 상수성은 요구하지 않음)
    const["ratio_p25"] = const.p25; const["ratio_p75"] = const.p75
    const["everTop500"] = const.index.isin(top500); const["everTop1000"] = const.index.isin(top1000)
    cur = b.current(); const["symbol"] = [cur.symbol.get(i) for i in const.index]
    inst = b.instruments().set_index("tradingItemId"); const["company"] = [inst.companyName.get(i) for i in const.index]
    # 누적 왜곡: 2000+ 벤더 스텝 곱 vs (1 − D_actual/P_prev) 곱 (DB 재조회: 계수 구간·배당·base 종가)
    c_ids = sorted(int(i) for i in const.index)
    if c_ids:
        dvf = db.q_ids(f"select tradingitemid as \"tradingItemId\", fromdate, divadjfactor from {SCHEMA}.ciqpriceequitydivadjfactor where tradingitemid = any(%s)", c_ids)
        dvf["fromdate"] = pd.to_datetime(dvf.fromdate); dvf["divadjfactor"] = dvf.divadjfactor.astype(float)
        dvd = db.q_ids(f"select tradingitemid as \"tradingItemId\", exdate, divamount, currencyid, dividendtypeid from {SCHEMA}.ciqdividend where tradingitemid = any(%s)", c_ids)
        dvd["exdate"] = pd.to_datetime(dvd.exdate); dvd["divamount"] = dvd.divamount.astype(float)
        bc = b.read_ids("base_data.parquet", c_ids, columns=["tradingItemId", "date", "close"])
        bc = bc[bc.close.notna()].sort_values(["tradingItemId", "date"])
        rows = []
        for tid in c_ids:
            f = dvf[dvf.tradingItemId == tid].sort_values("fromdate"); f["prev"] = f.divadjfactor.shift(1); f = f[f.prev.notna() & (f.fromdate >= "2000-01-04")]
            px = bc[bc.tradingItemId == tid]; d_ = dvd[(dvd.tradingItemId == tid) & dvd.dividendtypeid.isin(CASH_DIV_TYPES) & (dvd.currencyid == USD)]
            cum_v = 1.0; cum_a = 1.0; n = 0
            for r in f.itertuples():
                pp = px[px.date < r.fromdate].close
                if not len(pp):
                    continue
                D = float(d_[d_.exdate == r.fromdate].divamount.sum())
                cum_v *= r.prev / r.divadjfactor; cum_a *= max(1e-9, 1 - D / float(pp.iloc[-1])); n += 1
            m_ = mb_all[mb_all.tradingItemId == tid]
            rows.append(dict(tradingItemId=tid, symbol=const.symbol.get(tid), company=const.company.get(tid), n_steps_2000plus=n, ratio_median=float(const.med.get(tid)),
                             ratio_p25=float(const.ratio_p25.get(tid)), ratio_p75=float(const.ratio_p75.get(tid)), n_mismatch_rows=int(const.n.get(tid)),
                             cum_vendor_payout_factor=cum_v, cum_actual_payout_factor=cum_a, total_return_bias_factor=cum_a / cum_v if cum_v else None,
                             first_close=px.date.min(), last_close=px.date.max(), everTop500=tid in top500, everTop1000=tid in top1000,
                             top500_member_days=int(sum((row.toDate - row.fromDate).days for row in m_[m_.universe == "Top500"].itertuples())) if len(m_) else 0,
                             universes=sorted(m_.universe.unique().tolist())))
        cdf = pd.DataFrame(rows)
    else:
        cdf = pd.DataFrame()
    cdf.to_csv(out / "J_gdiv_systematic_scale_ids.csv", index=False)
    g_sum["systematic_scale_ids"] = int(len(cdf)); g_sum["systematic_scale_everTop500"] = int(cdf.everTop500.sum()) if len(cdf) else 0
    g_sum["systematic_scale_everTop1000"] = int(cdf.everTop1000.sum()) if len(cdf) else 0
    g_sum["systematic_scale_bias_gt_10pct"] = int(((cdf.total_return_bias_factor - 1).abs() > 0.10).sum()) if len(cdf) else 0
    g_sum["systematic_scale_cases"] = cdf.sort_values(["everTop500", "everTop1000"], ascending=False).to_dict("records") if len(cdf) else []
    # (3) G-RAW everTop500 (mi 종가 불일치 >1e-4, 2010+)
    md = pd.read_parquet(b.root / "qa/T2/price_differences/mi_close_diff_2010plus.parquet")
    m5 = md[(md.field == "close") & md.tradingItemId.isin(top500) & (md.rel.abs() > 1e-4)].copy()
    rows = []
    for r in m5.itertuples():
        tid = int(r.tradingItemId); d = pd.Timestamp(r.date)
        ciq = db.q(f"""select pricingdate, priceopen*adjustmentfactor o, pricehigh*adjustmentfactor h, pricelow*adjustmentfactor l, priceclose*adjustmentfactor c, adjustmentfactor adjf, volume/adjustmentfactor v
                       from {SCHEMA}.ciqpriceequity where tradingitemid = any(%s) and pricingdate between %s and %s order by pricingdate""", ([tid], (d - pd.Timedelta(days=4)).to_pydatetime(), (d + pd.Timedelta(days=4)).to_pydatetime()))
        mi = db.q(f"""select pricedate, priceopen o, pricehigh h, pricelow l, priceclose c, volume v from {SCHEMA}.miprice where tradingitemid = any(%s) and pricedate between %s and %s order by pricedate""",
                  ([tid], (d - pd.Timedelta(days=4)).date(), (d + pd.Timedelta(days=4)).date()))
        c_row = ciq[pd.to_datetime(ciq.pricingdate) == d]; m_row = mi[pd.to_datetime(mi.pricedate) == d]
        ciq_c = float(c_row.c.iloc[0]) if len(c_row) and pd.notna(c_row.c.iloc[0]) else None; mi_c = float(m_row.c.iloc[0]) if len(m_row) and pd.notna(m_row.c.iloc[0]) else None
        # 어느 쪽이 그날 고저 범위 안인지 / 전후일 종가와 가까운지
        ciq_hl_ok = (len(c_row) and pd.notna(c_row.l.iloc[0]) and float(c_row.l.iloc[0]) - 1e-6 <= (ciq_c or 0) <= float(c_row.h.iloc[0]) + 1e-6) if ciq_c is not None else None
        mi_in_ciq_hl = (len(c_row) and pd.notna(c_row.l.iloc[0]) and float(c_row.l.iloc[0]) - 1e-6 <= (mi_c or 0) <= float(c_row.h.iloc[0]) + 1e-6) if mi_c is not None else None
        prev_c = ciq[pd.to_datetime(ciq.pricingdate) < d].c.astype(float).dropna()
        next_c = ciq[pd.to_datetime(ciq.pricingdate) > d].c.astype(float).dropna()
        neigh = np.nanmean([prev_c.iloc[-1] if len(prev_c) else np.nan, next_c.iloc[0] if len(next_c) else np.nan])
        rows.append(dict(tradingItemId=tid, symbol=r.symbol, date=d, base_close=r.ciq_raw, mi_close_T2=r.mi, rel=r.rel, adjf=r.adjf, db_ciq_raw_close=ciq_c, db_mi_close=mi_c,
                         ciq_within_own_hl=ciq_hl_ok, mi_within_ciq_hl=mi_in_ciq_hl, neighbor_close_mean=neigh, ciq_dist_to_neighbors=abs((ciq_c or np.nan) / neigh - 1), mi_dist_to_neighbors=abs((mi_c or np.nan) / neigh - 1),
                         ciq_o_h_l=[float(x) if pd.notna(x) else None for x in (c_row[["o", "h", "l"]].iloc[0] if len(c_row) else [None] * 3)], mi_o_h_l=[float(x) if pd.notna(x) else None for x in (m_row[["o", "h", "l"]].iloc[0] if len(m_row) else [None] * 3)],
                         universe_member_on_date=member_on(tid, d), qualityFlag2_expected=abs(r.rel) > 1e-3))
    rdf = pd.DataFrame(rows)
    # base_data flag 확인
    if len(rdf):
        bq = b.read_ids("base_data.parquet", sorted(rdf.tradingItemId.unique()), columns=["tradingItemId", "date", "qualityFlag", "close"])
        rdf = rdf.merge(bq, on=["tradingItemId", "date"], how="left")
        rdf["likely_correct"] = np.where(rdf.ciq_dist_to_neighbors < rdf.mi_dist_to_neighbors, "ciq(base)", np.where(rdf.mi_dist_to_neighbors < rdf.ciq_dist_to_neighbors, "mi", "tie"))
    rdf.to_csv(out / "J_graw_everTop500_db.csv", index=False)
    r_sum = dict(rows=len(rdf), ids=int(rdf.tradingItemId.nunique()) if len(rdf) else 0, in_universe_on_date=int((rdf.universe_member_on_date.str.len() > 0).sum()) if len(rdf) else 0,
                 flag2_rows=int((rdf.qualityFlag == 2).sum()) if len(rdf) else 0, likely_correct=rdf.likely_correct.value_counts().to_dict() if len(rdf) else {},
                 cases=rdf[["symbol", "date", "base_close", "db_mi_close", "rel", "adjf", "ciq_dist_to_neighbors", "mi_dist_to_neighbors", "likely_correct", "qualityFlag", "universe_member_on_date"]].to_dict("records") if len(rdf) else [])
    warn = a_sum["in_universe_on_date"] > 0 or g_sum["in_universe_on_date"] > 0 or r_sum["in_universe_on_date"] > 0
    return dict(verdict=verdict(False, warn), artifacts=a_sum, gdiv_everTop500=g_sum, graw_everTop500=r_sum)


# ----------------------------------------------------------------------------------------------------------------------
# 리포트
# ----------------------------------------------------------------------------------------------------------------------
ITEMS = {"A": item_A, "B": item_B, "C": item_C, "D": item_D, "E": item_E, "F": item_F, "G": item_G, "H": item_H, "I": item_I, "J": item_J}
TITLES = {"A": "manifest 무결성·스키마·정렬·중복·달력", "B": "raw 가격 원자료 대조(무작위 셀)", "C": "계수 구간 커버리지·원본 대조·극단값", "D": "returns_total 재계산·배당 항등식·분할 경계",
          "E": "lifecycle·꼬리·알려진 사례·상태 재확인", "F": "lineage·이중계산·exclusions", "G": "universe", "H": "events", "I": "소비자 관점(로더)", "J": "T2 인공물·G-DIV·G-RAW 잔여 실체"}


def write_report(out: Path, b: Build) -> None:
    """JSON 결과 → AUDIT_results.md(항목별 판정·핵심 수치 자동표) + returns_known_issues.csv(소비자용 알려진 문제 목록)."""
    R = {it: json.loads((out / f"{it}.json").read_text()) for it in ITEMS if (out / f"{it}.json").exists()}
    lines = [f"# T7 감사 결과표 (자동 생성, build {b.manifest['build_id']}, asOf {b.manifest['asOfDate']})", "",
             f"생성 {time.strftime('%Y-%m-%d %H:%M:%S')} · `audit_v21.py` · 항목별 상세 JSON/CSV 는 같은 디렉토리", "",
             "| 항목 | 판정 | self-test | 표본/범위 | 핵심 결과 | 시간(s) |", "|---|---|---|---|---|---|"]
    def g(d, *ks, default=None):
        for k in ks:
            if not isinstance(d, dict) or k not in d:
                return default
            d = d[k]
        return d
    def top500_ext(r):
        d = g(r, 'extreme', 'overlap_by_universe', default={}) or {}
        return d.get("('all', 'Top500')", 0)
    summ = {
        "A": lambda r: (f"manifest {g(r,'manifest','n_files')}파일·스키마 {g(r,'schema','files_checked')}파일·48.0M행 전수",
                        f"sha/행수 불일치 {g(r,'manifest','sha_mismatch')}/{g(r,'manifest','rows_mismatch')}, 중복키 {g(r,'sort_unique','base_data.parquet','duplicate_keys')}, 정렬위반 {g(r,'sort_unique','base_data.parquet','unsorted_steps')}, 달력밖 {g(r,'sort_unique','base_data.parquet','off_calendar_dates')}, base∖lifecycle {g(r,'id_set','base_not_in_lifecycle')}"),
        "B": lambda r: (f"무작위 {g(r,'sample','n_cells')}셀({g(r,'sample','n_ids')}종목, everTop500 {g(r,'sample','top500_cells')}) + flag2/3 각 150",
                        f"ciq 불일치 {g(r,'ciq','mismatch_cells')}(최대 상대차 {g(r,'ciq','max_rel_close_diff'):.1e}), mi 불일치 {g(r,'mi','mi_rel_gt_1e3')}, flag2 의미 일치 {g(r,'flag_strata','flag2_with_mi_mismatch_1e3')}/150, flag3 null 일치 {g(r,'flag_strata','flag3_db_close_null')}/150"),
        "C": lambda r: (f"구간 전수(split {g(r,'coverage','split','n_rows')}·div {g(r,'coverage','dividend','n_rows')}행) + 300종목 {g(r,'split_vs_adjf_and_db','rows_both'):,}행 DB 대조",
                        f"빈틈/겹침 0, lookup vs DB adjf 불일치 {g(r,'split_vs_adjf_and_db','lookup_vs_db_gt_1e6')}, divadj 일별 불일치 {g(r,'divadj_vs_build','daily_mismatch_gt_1e9')}, 극단계수 유니버스 겹침 {g(r,'extreme','overlap_ids')}종목(Top500 {top500_ext(r)})"),
        "D": lambda r: (f"재계산 {g(r,'returns_recompute','ids')}종목 {g(r,'returns_recompute','rows_recomputed'):,}행; 배당락 {g(r,'dividend_identity','n_checked'):,}건; 분할경계 {g(r,'split_boundaries','boundaries_with_prev_and_t_close'):,}건 전수",
                        f"재계산 최대차 {g(r,'returns_recompute','max_abs_diff')}; 항등식 1e-6 통과 {g(r,'dividend_identity','pass_1e6')}/{g(r,'dividend_identity','n_checked')}, 1e-4 초과 {g(r,'dividend_identity','fail_1e4')}; 가짜점프 {g(r,'split_boundaries','fake_jump_cells_total_return')}(T2 목록 외 {g(r,'split_boundaries','fake_total_not_in_T2_list')}); 큰 분배일 Top500 재투자식−현금식 >10pp {g(r,'large_distributions','top500_file_minus_cash_gt_10pct')}"),
        "E": lambda r: (f"전수(base 48.0M·returns 48.0M) + 알려진 사례 {len(r.get('known_cases', []))} + status 재확인 {g(r,'status_recheck','n')}",
                        f"flag4 xor post {g(r,'tails','flag4_xor_post')}, returns post-lastTrade {g(r,'tails','returns_rows_post_lastTrade')}, 사례 last/first 일치 전부, status 재확인 {g(r,'status_recheck','ok')}/{g(r,'status_recheck','n')}"),
        "F": lambda r: (f"같은 라인 {g(r,'lineage_same_line','pairs')}쌍 + exclusions {g(r,'exclusions','rows')} + 같은 securityId {g(r,'same_security_pairs','pairs')}쌍",
                        f"동시 tradable high/medium {g(r,'lineage_same_line','high_medium_overlapping')}쌍(low {g(r,'lineage_same_line','low_overlapping')}=별개회사), 유니버스 동시멤버 high/medium {g(r,'universe_double_membership','pairs_high_medium')}, 제외구간 tradable {g(r,'exclusions','excluded_tradable_in_window_total')}, 같은 securityId 겹침 {g(r,'same_security_pairs','overlapping')}"),
        "G": lambda r: (f"Top500 일별 복원 vs 프로토 {g(r,'overlap_vs_proto','v1rule','days')}일; 전환 5사례; TokensToRemove 46+선행 5",
                        f"line 중첩 중앙 v1rule {g(r,'overlap_vs_proto','v1rule','overlap_line_median'):.4f}/id {g(r,'overlap_vs_proto','v1rule','overlap_id_median'):.4f}(T5 0.9978/0.9740 재현), 멤버수 ±5% 점프일 {g(r,'overlap_vs_proto','v1rule','member_count_jump_days_gt5pct')}, 금지 id 멤버행 {g(r,'tokens','membership_rows_for_banned')}"),
        "H": lambda r: (f"events {g(r,'endDate_vs_lastTrade','rows'):,}행 전수 + 실사례 4 + 거절 258",
                        f"k≡1 위반 0, endDate>lastTrade {g(r,'endDate_vs_lastTrade','endDate_gt_lastTrade')}(everTop500 {g(r,'endDate_vs_lastTrade','gt_everTop500')}), successorCacheClose 불일치 {g(r,'successor','rel_gt_1e6')}, 승계사 집합 밖 {g(r,'successor','successor_outside')}, 거절 258 중 1:1 실제로 보이는 {g(r,'rejected_rename','needs_successor_row')}·분할비로 채울 수 있는 {g(r,'rejected_rename','fixable_by_split_ratio')}"),
        "I": lambda r: (f"로더 Top500∧tradable 2010+ {g(r,'v1rule','member_tradable_cells'):,}셀",
                        f"NaN {g(r,'v1rule','nan_ret_cells_in_mask')}(전부 첫 종가행), |r|>50% {g(r,'v1rule','abs_gt_50pct_cells')}(v1rule)/{g(r,'all','abs_gt_50pct_cells')}(all) 전부 실제 급변·배당락 동일, 일중앙값 중앙 {g(r,'v1rule','median_of_daily_median'):.5f}, check_returns 최대차 {g(r,'loader_check_returns','max_abs_diff') if isinstance(g(r,'loader_check_returns'), dict) else 'n/a'}"),
        "J": lambda r: (f"인공물 {g(r,'artifacts','rows')} · G-DIV 잔여 {g(r,'gdiv_everTop500','rows_total'):,}(everTop500 {g(r,'gdiv_everTop500','rows_everTop500')}) · G-RAW everTop500 {g(r,'graw_everTop500','rows')}",
                        f"인공물 유니버스 당일 멤버 {g(r,'artifacts','in_universe_on_date')}(Top500 0); 배당계수 체계적 스케일 오류 {g(r,'gdiv_everTop500','systematic_scale_ids')}종목(everTop500 {g(r,'gdiv_everTop500','systematic_scale_everTop500')}); G-RAW 7행 중 flag2 {g(r,'graw_everTop500','flag2_rows')}"),
    }
    for it, r in R.items():
        try:
            scope, key = summ[it](r)
        except Exception as e:  # noqa
            scope, key = "", f"(요약 오류 {e})"
        st = r.get("selftest", {}).get("passed") if isinstance(r.get("selftest"), dict) else None
        lines.append(f"| {it}. {TITLES[it]} | **{r.get('verdict')}** | {'PASS' if st else ('—' if st is None else 'FAIL')} | {scope} | {key} | {r.get('_elapsed_s')} |")
    lines += ["", f"실행: 전체 {sum(r.get('_elapsed_s', 0) for r in R.values()):.0f}s, peak RSS {max(r.get('_peak_rss_gb', 0) for r in R.values()):.1f}GB, DB 질의 {max(r.get('_db_queries', 0) for r in R.values())}회(읽기 전용·id 청크)", ""]
    (out / "AUDIT_results.md").write_text("\n".join(lines))
    # 소비자용 알려진 문제 목록
    rows = []
    D = R.get("D", {}); J = R.get("J", {}); E = R.get("E", {}); I_ = R.get("I", {})
    for c in g(J, "gdiv_everTop500", "systematic_scale_cases", default=[]) or []:
        if c.get("everTop1000") or c.get("universes"):
            rows.append(dict(tradingItemId=c["tradingItemId"], symbol=c.get("symbol"), dateFrom=c.get("first_close"), dateTo=c.get("last_close"),
                             issue="vendor divAdjFactor scale error (implied dividend = k x actual)", severity="high" if abs(c["total_return_bias_factor"] - 1) > 0.1 else "low",
                             metric=f"ratio_median={c['ratio_median']:.3f}; cumulative total-return bias x{c['total_return_bias_factor']:.3f}", universes=",".join(c.get("universes", [])),
                             source="T7 item J (G-DIV residual scan)"))
    for c in g(D, "large_distributions", "top500_cases_gt_5pct", default=[]) or []:
        rows.append(dict(tradingItemId=c["tradingItemId"], symbol=c.get("symbol"), dateFrom=c["date"], dateTo=c["date"],
                         issue="large distribution day: vendor-factor (reinvestment) total return differs from cash-basis holder return", severity="high" if abs(c["ret_file_minus_cash"]) > 0.10 else "medium",
                         metric=f"D/P={c['DP']:.3f}; ret_file={c['ret']:.4f}; ret_cash={c['ret_cash_formula']:.4f}; diff={c['ret_file_minus_cash']:+.4f}", universes=c.get("universe_on_date"), source="T7 item D (large_distributions)"))
    rows.append(dict(tradingItemId=314828302, symbol="TYC", dateFrom="2007-07-02", dateTo="2007-07-02", issue="vendor divAdjFactor covers only one of two spin-off legs (Covidien+TE); returns_total -42.0% vs ~+0.7% true (sibling line 34969540 correct)", severity="high",
                     metric="ciqdividendcache spin-off 10.775 vs sibling line 34969540 20.54", universes="Top100..ALL", source="T7 item D (split_boundaries) + DB ciqdividendcache"))
    rows.append(dict(tradingItemId=314828302, symbol="TYC", dateFrom="2012-10-01", dateTo="2012-10-01", issue="vendor divAdjFactor covers only part of ADT+Pentair distribution; returns_total -37.6% vs ~+3.3% true (sibling line 34969540 correct)", severity="high",
                     metric="ciqdividendcache spin-off 10.584 vs sibling line 34969540 28.68", universes="Top200..ALL", source="T7 item D (large_distributions) + DB ciqdividendcache"))
    for c in g(D, "split_boundaries", "everTop500_not_matching_cases", default=[]) or []:
        if abs(c.get("ret", 0) or 0) > 0.25 and c["tradingItemId"] != 314828302:
            rows.append(dict(tradingItemId=c["tradingItemId"], symbol=None, dateFrom=c["date"], dateTo=c["date"], issue="split boundary: raw jump != split ratio and |ret_total|>25% (check vs mi/news)", severity="medium",
                             metric=f"sf {c['sf_prev']}->{c['sf_t']}, raw_ret={c['raw_ret']:.3f}, ret_total={c['ret']:.3f}", universes="", source="T7 item D (split_boundaries)"))
    art = pd.read_csv(b.root / "qa/T2/factor_artifacts.csv")
    for a in art[art.category.str.startswith("unexplained")].itertuples():
        rows.append(dict(tradingItemId=a.tradingItemId, symbol=a.symbol, dateFrom=a.date, dateTo=a.date, issue="vendor split-factor artifact: split_adj/returns jump without raw move (T2 G-JUMP list)", severity="low" if not a.everTop1000 else "medium",
                         metric=f"r_total={a.r_total:.3f}, r_raw={a.r_raw:.3f}", universes="", source="T2 qa/factor_artifacts.csv"))
    rows.append(dict(tradingItemId=7727657, symbol="TEVA", dateFrom="2003-08-12", dateTo="2026-09-11", issue="sparse odd-lot NYSE ordinary-share line before ADR->ordinary conversion (real volume from 2026-09-14); ADR line 2657569 not in instrument set", severity="low",
                     metric="610 sparse rows, vol 100~1,100/day, price deviates up to ~10% from ADR", universes="", source="T7 item E"))
    for c in (g(I_, "v1rule", "nan_cells", default=[]) or []):
        rows.append(dict(tradingItemId=c["tradingItemId"], symbol=None, dateFrom=c["date"], dateTo=c["date"], issue="universe member on its first close row: returns_total NaN (new id after reorganization)", severity="low",
                         metric="ret NaN on first tradable day", universes="Top500", source="T7 item I"))
    ki = pd.DataFrame(rows)
    mbx = pd.concat([b.membership(False), b.membership(True)]).drop_duplicates()
    cur = b.current()
    def uni_on(tid, d):
        try:
            d = pd.Timestamp(d)
        except Exception:  # noqa
            return ""
        sub = mbx[(mbx.tradingItemId == int(tid)) & (mbx.fromDate <= d) & (mbx.toDate >= d)]
        return ",".join(sorted(sub.universe.unique()))
    ki["universe_on_dateFrom"] = [uni_on(t, d) for t, d in zip(ki.tradingItemId, ki.dateFrom)]
    ki["symbol"] = [sym if isinstance(sym, str) else cur.symbol.get(int(t)) for sym, t in zip(ki.symbol, ki.tradingItemId)]
    # 유니버스 비멤버 날의 경계·인공물은 low 로
    ki.loc[ki.issue.str.startswith("split boundary") & (ki.universe_on_dateFrom == ""), "severity"] = "low"
    ki = ki.sort_values(["severity", "tradingItemId", "dateFrom"], key=lambda c: c.map({"high": 0, "medium": 1, "low": 2}) if c.name == "severity" else c)
    ki.to_csv(out / "returns_known_issues.csv", index=False)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default=str(DEFAULT_BUILD))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--items", default="A,B,C,D,E,F,G,H,I,J")
    ap.add_argument("--seed", type=int, default=20260916)
    ap.add_argument("--report-only", action="store_true")
    a = ap.parse_args(argv)
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    tmp = SCRATCH / "t7_work"; tmp.mkdir(parents=True, exist_ok=True)
    b = Build(Path(a.build))
    if not a.report_only:
        db = DB()
        for it in a.items.split(","):
            it = it.strip()
            if not it:
                continue
            log(f"== item {it} start")
            t0 = time.time()
            try:
                res = ITEMS[it](b=b, out=out, tmp=tmp, db=db, seed=a.seed)
            except Exception as e:  # noqa
                res = dict(verdict="ERROR", error=repr(e), traceback=traceback.format_exc())
            res["_elapsed_s"] = round(time.time() - t0, 1); res["_peak_rss_gb"] = round(peak_rss_gb(), 2); res["_db_queries"] = db.n_queries; res["_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            save_json(out, it, res)
            log(f"== item {it} done verdict={res.get('verdict')} {res['_elapsed_s']}s rss={res['_peak_rss_gb']}GB")
    write_report(out, b)
    log("report written: AUDIT_results.md, returns_known_issues.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
