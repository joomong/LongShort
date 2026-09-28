#!/home/jgo/usr/venv-3.11/bin/python
"""cache_v21/rc2f_apply.py - rc2f X0: 교정 CSV(orch_rc2f/BRIEF.md §3) 검증기 + 적용기.

서브커맨드
  validate <csv> [<csv>...] [--rc2 <build_rc2_final>]
      §3 스키마 검증. 파일 종류는 파일명(split_/price_/ledger_/lifecycle_/lineage_*.csv)으로 판별. 필수 컬럼, overrideId 유일,
      action/enum 값, 날짜 형식(YYYY-MM-DD), set JSON 파싱·키·값, tradingItemId 가 base_data/master 에 존재, ledger eventId 존재
      (set/drop; stitched_copy 행은 keeper 를 가리켜야 함), 같은 대상 중복 교정(파일 간 포함). 결과 표 출력, 오류가 있으면 exit 1.
  apply --rc2 <build_rc2_final> --stage-rc2 <stage_rc2> --overrides-glob '<glob>' --out <stage_rc2f/build> [--stage-v21 <stage_v21>] [--no-ledger]
      §4 X0 적용 순서: price → factors/split → lifecycle/events/lineage → ledger_build --overrides → (rc2f_build.sh 가 이어서
      publish(returns v2 + export) → ledger_qa → returns_v2_qa → rc2f_recount). 출력 레이아웃은 publish.py --rc2-stage 가 읽는
      R1/R3 배치 그대로(+ T2/ 교정 base_data·factors, publish.py --t2 로 공급). 적용 로그 <out>/rc2f_apply_log.csv.

교정 종류별로 바뀌는 파일·컬럼(apply 단계, 자세한 규칙은 각 함수 docstring):
  price_overrides     T2/base_data.parquet 또는 R3/base_data_aux.parquet 의 (id, dateFrom..dateTo) 행: fields(close,open,high,low,vwap 기본)
                      에 multiply/set_close, qualityFlag = 8 (QF_PRICE_OVERRIDE; 기존 0~4 enum 과 충돌 없음). volume 불변.
  split_overrides     T2/factors/split.parquet(또는 R3/factors_aux/split.parquet) 구간: drop = 경계 제거(경계 이전 구간 factor ×
                      factorNext/factorPrev, 같은 factor 인접 구간 병합), set_factor = 경계 이전 구간 factor × (newFactor×factorNext/factorPrev),
                      move_date = 경계 validFrom/validTo 이동, add = 경계 삽입(이전 구간 × newFactor), confirm = 변경 없음(no_op).
                      원장 split 행은 ledger_build 가 교정된 factors 로 재생성(R1/root/factors/split.parquet).
  lifecycle_overrides R3/master/lifecycle.parquet(lastTradeDate·lastTradeSource·endReason·evidence), R3/master/lifecycle_dates.parquet
                      (lastTradeDate/effectiveDate/settlementDate/payDate + *Source='rc2f_override:<id>'), R3/events/corporate_actions_reclass
                      .parquet(endDate·settlementDate·cashPerShare·successorTradingItemId·successorRatio·hasCashLeg·hasStockLeg·
                      settlementReturn·endDateSource='override'·overrideSource), R3/master/lineage.parquet(convert_successor → merger 링크
                      ratio/effectiveDate). 가격 꼬리 컷은 publish 의 lifecycle 마스킹이 새 lastTradeDate 로 수행.
  lineage_overrides   R3/master/lineage.parquet: set_ratio(ratio, ratioSource), drop(행 제거), add(행 추가), set_chainable(chainable).
  ledger_overrides    ledger_build.py --overrides: MANUAL_OVERRIDES 와 같은 메커니즘(match/set/add/drop; repairs 로그에 overrideId),
                      평가(value_children) 뒤 status/isEstimate/childValuePerOldShare/childValueSource 는 post-pass 로 재확정.
  value_overrides     T2/base_data.parquet 또는 R3/base_data_aux.parquet 의 (id, date) 행 field(open,high,low,close,vwap,volume) = value,
                      기존 행의 qualityFlag 는 그대로(열거형 0~4·8·16·32 와 섞지 않음; 근거는 apply log). 행이 없으면 거래일이고
                      lifecycle [firstTradeDate, lastTradeDate] 안일 때만 새 행(qualityFlag 64 = QF_VALUE_OVERRIDE, 지정 안 된 필드 NaN).
                      factors·다른 테이블 불변.
                      한계: (1) 기존 행의 flag 는 바뀌지 않음(예: flag 3 행에 close 를 넣어도 3 유지).
                      (2) 새 행의 lifecycle 창은 같은 빌드의 lifecycle_overrides 적용 전 값으로 판정.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import logging
import os
import re
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

SP = Path(os.environ.get("RC2F_SP", "/tmp/claude-1008/-home-jgo/3f2abe1d-a2a7-4017-9ac1-2c7cb961e311/scratchpad"))   # R-X0 P2-5: env override
RC2_DEFAULT = SP / "build_rc2_final"
STAGE_RC2_DEFAULT = SP / "stage_rc2"
STAGE_V21_DEFAULT = SP / "stage_v21"
PY = "/home/jgo/usr/venv-3.11/bin/python"
START = pd.Timestamp("2000-01-01")
QF_PRICE_OVERRIDE = 8            # base_data.qualityFlag: rc2f 수동 단위 교정(0 confirmed/1 provisional/2 mi_mismatch/3 ciq_null_close/4 post_lifecycle 와 별개)
QF_PRICE_OVERRIDE_EST = 32       # base_data.qualityFlag: 추정 가격 교정(price_overrides isEstimate true, 예: 옛 단위 프린트 ×1/pf) — returns 행 retFlag ≥ 3, 원장 split 행 estimated (R-Y2 P1 ①)
QF_SUPPLEMENTED = 16             # base_data.qualityFlag: rc2f extend_prices 로 xfeed 에서 보충한 행(source 1 = ciqpriceequity, 2 = miprice)
QF_VALUE_OVERRIDE = 64           # base_data.qualityFlag: value_overrides 로 새로 만든 행(기존 행의 flag 는 바꾸지 않음)
SOURCE_CIQ, SOURCE_MI = 1, 2

KINDS = ("split", "price", "ledger", "lifecycle", "lineage", "aux", "value")
COMMON_COLS = ["overrideId", "caseId", "tradingItemId", "evidence", "source", "isEstimate", "owner"]
KIND_COLS = {
    "split": ["exDate", "action", "newFactor", "newExDate"],          # 선택: checkBasis (Y2a)
    "price": ["dateFrom", "dateTo", "op", "value", "fields"],
    "ledger": ["eventId", "match_tradingItemId", "match_exDate", "match_legType", "match_childTradingItemId", "action", "set"],
    "lifecycle": ["lastTradeDate", "effectiveDate", "settlementDate", "payDate", "endReason", "successorTradingItemId", "successorRatio",
                  "cashPerShare", "treatment"],
    "lineage": ["oldId", "newId", "action", "ratio", "effectiveDate", "relation", "chainable"],      # 선택: linkVerified (rc2f-E)
    "aux": [],
    "value": ["date", "field", "value"],
}
# ledger 파일에서는 eventId 또는 match_* 중 하나만 있으면 되므로 필수 컬럼은 action/set 만
REQUIRED_COLS = {k: COMMON_COLS + KIND_COLS[k] for k in KINDS}
REQUIRED_COLS["ledger"] = COMMON_COLS + ["action", "set"]
REQUIRED_COLS["aux"] = ["overrideId", "tradingItemId", "evidence", "owner"]        # aux_additions.csv (X3 인계): 보조 종목 추가 수집
SPLIT_ACTIONS = ("drop", "set_factor", "move_date", "add", "confirm")
PRICE_OPS = ("multiply", "set_close")
PRICE_FIELDS = ("close", "open", "high", "low", "vwap")
VALUE_FIELDS = ("open", "high", "low", "close", "vwap", "volume")
LEDGER_ACTIONS = ("set", "drop", "add")
LEDGER_SET_KEYS = ("childRatio", "childTradingItemId", "childValuePerOldShare", "childValueSource", "cashPerShare", "cashCurrency", "eventType",
                   "legType", "status", "isEstimate", "exDate", "payDate", "subscriptionPrice", "expiryDate", "exerciseRatio", "notes", "parentFactor")
LEDGER_POST_KEYS = ("childValuePerOldShare", "childValueSource", "status", "isEstimate")   # 평가 뒤 재확정하는 키
LEDGER_DATE_KEYS = ("exDate", "payDate", "expiryDate")
LEDGER_NUM_KEYS = ("childRatio", "childValuePerOldShare", "cashPerShare", "subscriptionPrice", "exerciseRatio", "parentFactor")
EVENT_TYPES = ("cash_dividend", "special_dividend", "stock_dividend", "split", "spin_off", "rights", "warrant", "merger_cash", "merger_stock",
               "rename", "relist", "other")
LEG_TYPES = ("cash", "stock", "quantity", "rights")
STATUSES = ("ok", "estimated", "unresolved")
VALUE_SOURCES = ("child_close_ex", "wi_close", "event_value", "vendor_implied", "unknown")
LEG_OF_TYPE = {"cash_dividend": ("cash",), "special_dividend": ("cash",), "stock_dividend": ("quantity",), "split": ("quantity",),
               "spin_off": ("stock",), "rights": ("rights", "stock"), "warrant": ("rights", "stock"), "merger_cash": ("cash",),
               "merger_stock": ("stock",), "rename": ("quantity",), "relist": ("quantity",), "other": ("cash", "stock", "quantity", "rights")}
MERGER_TYPES = ("merger_cash", "merger_stock", "other")
END_REASONS = ("cash_merger", "stock_merger", "mixed_merger", "id_change", "halt_suspension", "bankruptcy", "liquidation", "data_gap",
               "delist_other", "still_trading")
TREATMENTS = ("settle_cash", "convert_successor", "settle_mixed", "last_close", "unresolved", "extend_prices")
LINEAGE_ACTIONS = ("set_ratio", "drop", "add", "set_chainable")
LINEAGE_RELATIONS = ("rename", "relist", "merger", "unknown")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
OVERRIDE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*-\d+$")

log = logging.getLogger("cache_v21.rc2f_apply")


# --------------------------------------------------------------------------------------
# 공통 유틸
# --------------------------------------------------------------------------------------
def task_dir(path) -> str:
    """교정 파일의 태스크 폴더 이름: …/<task>/overrides/<file> 이면 <task>, …/<task>/<file>(저장소 overrides/rc2f/<task>/) 이면 <task>."""
    p = Path(path)
    return p.parent.parent.name if p.parent.name == "overrides" else p.parent.name


def file_label(path) -> str:
    """표시용 파일 이름: <task>/<file>."""
    return f"{task_dir(path)}/{Path(path).name}"


def kind_of(path: Path) -> str | None:
    name = Path(path).name.lower()
    for k in KINDS:
        if name.startswith(k + "_"):
            return k
    return None


def read_override_csv(path: Path) -> pd.DataFrame:
    """전부 문자열로 읽는다(빈 셀 = ''); 컬럼명 앞뒤 공백 제거."""
    df = pd.read_csv(path, dtype=str, keep_default_na=False, na_filter=False, encoding="utf-8-sig")
    df.columns = [str(c).strip() for c in df.columns]
    for c in df.columns:
        df[c] = df[c].astype(str).str.strip()
    df["x_row"] = np.arange(2, len(df) + 2)          # 파일의 줄 번호(헤더 = 1)
    df["x_file"] = str(path)
    return df


def parse_date(s: str):
    """YYYY-MM-DD 만 허용. 반환 Timestamp 또는 None(빈 값) 또는 ValueError."""
    s = (s or "").strip()
    if not s:
        return None
    if not DATE_RE.match(s):
        raise ValueError(f"date '{s}' is not YYYY-MM-DD")
    return pd.Timestamp(s)


def parse_bool(s: str, default=None):
    v = (s or "").strip().lower()
    if v in ("nan", "none", "null"):      # pandas-written missing values in optional bool columns
        v = ""
    if v in ("true", "1", "1.0", "yes", "y", "t"):
        return True
    if v in ("false", "0", "0.0", "no", "n", "f"):
        return False
    if v == "" and default is not None:
        return default
    raise ValueError(f"bool '{s}' must be true/false")


def parse_int(s: str):
    v = (s or "").strip()
    if v == "":
        return None
    try:
        f = float(v)
    except ValueError as e:
        raise ValueError(f"int '{s}' not numeric") from e
    if not np.isfinite(f) or abs(f - round(f)) > 0:
        raise ValueError(f"int '{s}' not an integer")
    return int(round(f))


def parse_float(s: str):
    v = (s or "").strip()
    if v == "":
        return None
    try:
        f = float(v)
    except ValueError as e:
        raise ValueError(f"number '{s}' not numeric") from e
    return f


class Ref:
    """검증·적용 참조 데이터(rc2 발행 빌드, 읽기 전용). 지연 로드."""

    def __init__(self, rc2: Path):
        self.rc2 = Path(rc2)
        self._cache = {}
        self.pending_aux_ids: set = set()          # ids listed in aux_additions.csv of the same validate/apply run (collected before lifecycle/ledger steps)
        self.pending_lineage_ratios: dict = {}     # (oldId, newId) -> (ratio, overrideId) from lineage_overrides set_ratio/add of the same run (R-X4 §4-9)
        self.pending_price_ids: set = set()        # ids with a price override in the same run (Y2a: the validator's raw-jump check on rc2 prices is deferred to apply)

    def _get(self, key, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    @property
    def lifecycle(self) -> pd.DataFrame:
        return self._get("lifecycle", lambda: pd.read_parquet(self.rc2 / "master" / "lifecycle.parquet").set_index("tradingItemId"))

    @property
    def ids(self) -> set:
        def load():
            ids = set(int(i) for i in self.lifecycle.index)
            import hashlib as _h
            durable = self.rc2 / "base_data_ids.parquet"        # D1 (R-D1 P1-1): slim durable reference dir (ledger_incremental.py make-ref) carries the id list
            cache = durable if durable.exists() else self.rc2.parent / "stage_rc2f" / "X0" / f"base_data_ids_{_h.sha1(str(self.rc2.resolve()).encode()).hexdigest()[:8]}.parquet"
            if cache.exists():
                ids |= set(pd.read_parquet(cache).tradingItemId.astype(int))
            else:
                pf = pq.ParquetFile(self.rc2 / "base_data.parquet")
                got = []
                for rg in range(pf.num_row_groups):
                    got.append(np.unique(pf.read_row_group(rg, columns=["tradingItemId"]).column(0).to_numpy()))
                u = np.unique(np.concatenate(got)) if got else np.zeros(0, np.int64)
                try:
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    pd.DataFrame({"tradingItemId": u}).to_parquet(cache, index=False)
                except OSError:
                    pass
                ids |= set(int(i) for i in u)
            return ids
        return self._get("ids", load)

    @property
    def price_range(self) -> pd.DataFrame:
        """id 별 base_data 관측 범위(첫/마지막 close 날짜)와 상장 범위(lifecycle)."""
        def load():
            lc = self.lifecycle
            first = pd.to_datetime(lc.firstPriceDate) if "firstPriceDate" in lc.columns else pd.to_datetime(lc.firstTradeDate)
            last = pd.concat([pd.to_datetime(lc[c]) for c in ("ciqLastCloseDate", "miLastCloseDate", "lastTradeDate") if c in lc.columns], axis=1).max(axis=1)
            return pd.DataFrame({"first": first, "last": last})
        return self._get("price_range", load)

    @property
    def ledger(self) -> pd.DataFrame:
        return self._get("ledger", lambda: pd.read_parquet(self.rc2 / "events" / "ca_ledger.parquet"))

    @property
    def ledger_ids(self) -> set:
        return self._get("ledger_ids", lambda: set(int(i) for i in self.ledger.tradingItemId.unique()))

    @property
    def ledger_by_id(self) -> dict:
        return self._get("ledger_by_id", lambda: self.ledger.set_index("eventId").to_dict("index"))

    @property
    def calendar(self):
        def load():
            import ledger_build as lb
            return lb.Calendar.from_root(self.rc2)
        return self._get("calendar", load)

    @property
    def split(self) -> pd.DataFrame:
        return self._get("split", lambda: pd.read_parquet(self.rc2 / "factors" / "split.parquet"))

    def closes_for(self, ids):
        """RawCloses of the rc2 base_data (+aux) for the given ids (Y2a validator raw-jump check); rebuilt when new ids are needed."""
        import ledger_build as lb
        need = set(int(i) for i in ids) & set(int(i) for i in self.ids)        # ids outside base_data are rejected by the schema checks anyway
        have = self._cache.get("closes_ids", set())
        if "closes" not in self._cache or not need <= have:
            if not (self.rc2 / "base_data.parquet").exists():
                if "closes" in self._cache:
                    return self._cache["closes"]
                raise FileNotFoundError(self.rc2 / "base_data.parquet")
            allids = have | need
            self._cache["closes"] = lb.RawCloses([self.rc2 / "base_data.parquet", self.rc2 / "base_data_aux.parquet"], allids)
            self._cache["closes_ids"] = allids
        return self._cache["closes"]

    @property
    def split_boundaries(self) -> pd.DataFrame:
        def load():
            import ledger_build as lb
            b = lb.split_boundaries(self.split)
            al, _ = self.calendar.align(b.validFrom.to_numpy())
            b["exDate"] = al
            return b
        return self._get("split_boundaries", load)

    @property
    def lineage(self) -> pd.DataFrame:
        return self._get("lineage", lambda: pd.read_parquet(self.rc2 / "master" / "lineage.parquet"))

    @property
    def lifecycle_dates(self) -> pd.DataFrame:
        return self._get("lifecycle_dates", lambda: pd.read_parquet(self.rc2 / "master" / "lifecycle_dates.parquet").set_index("tradingItemId"))

    @property
    def events(self) -> pd.DataFrame:
        return self._get("events", lambda: pd.read_parquet(self.rc2 / "events" / "corporate_actions.parquet").set_index("tradingItemId"))


# --------------------------------------------------------------------------------------
# validate
# --------------------------------------------------------------------------------------
class Issues:
    def __init__(self):
        self.rows = []

    def add(self, level: str, file: str, row, override_id: str, msg: str):
        self.rows.append({"level": level, "file": file_label(file), "row": row, "overrideId": override_id, "message": msg})

    def err(self, file, row, oid, msg):
        self.add("ERROR", file, row, oid, msg)

    def warn(self, file, row, oid, msg):
        self.add("WARN", file, row, oid, msg)

    @property
    def n_err(self) -> int:
        return sum(1 for r in self.rows if r["level"] == "ERROR")

    @property
    def n_warn(self) -> int:
        return sum(1 for r in self.rows if r["level"] == "WARN")


def _common_checks(df: pd.DataFrame, kind: str, ref: Ref, iss: Issues) -> pd.DataFrame:
    """공통 컬럼·타입. 반환: 파싱 결과 컬럼(x_tid, x_isEstimate) 을 붙인 df."""
    f = df["x_file"].iloc[0] if len(df) else ""
    missing = [c for c in REQUIRED_COLS[kind] if c not in df.columns]
    if missing:
        iss.err(f, 1, "", f"missing required columns {missing} (have {list(c for c in df.columns if not c.startswith('x_'))})")
        return None
    if kind == "aux":
        for c in ("caseId", "source", "isEstimate"):
            if c not in df.columns:
                df[c] = ""
    if kind == "ledger":
        has_eid = "eventId" in df.columns
        has_match = all(c in df.columns for c in ("match_tradingItemId", "match_exDate", "match_legType"))
        if not has_eid and not has_match:
            iss.err(f, 1, "", "ledger file needs eventId or match_tradingItemId/match_exDate/match_legType columns")
            return None
        for c in ("eventId", "match_tradingItemId", "match_exDate", "match_legType", "match_childTradingItemId"):
            if c not in df.columns:
                df[c] = ""
    tids, ests = [], []
    for r in df.itertuples(index=False):
        oid = r.overrideId
        if not oid:
            iss.err(f, r.x_row, oid, "overrideId empty")
        elif not OVERRIDE_ID_RE.match(oid):
            iss.warn(f, r.x_row, oid, "overrideId should look like <TASK>-<n>")
        if not r.caseId:
            iss.warn(f, r.x_row, oid, "caseId empty (registry link)")
        try:
            tid = parse_int(r.tradingItemId)
        except ValueError as e:
            iss.err(f, r.x_row, oid, f"tradingItemId: {e}")
            tid = None
        if tid is None:
            if kind != "lineage":
                iss.err(f, r.x_row, oid, "tradingItemId empty")
        elif kind == "aux":
            if tid in ref.ids:
                iss.warn(f, r.x_row, oid, f"tradingItemId {tid} already in base_data/master (aux addition will be a no-op)")
        elif tid not in ref.ids:
            if kind == "ledger" and str(getattr(r, "action", "")).strip() in ("drop", "set") and tid in ref.ledger_ids:
                iss.warn(f, r.x_row, oid, f"tradingItemId {tid} not in base_data/master but present in the rc2 ledger (dead id): {getattr(r, 'action', '')} allowed")
            else:
                iss.err(f, r.x_row, oid, f"tradingItemId {tid} not in base_data/master (rc2 build)")
        tids.append(tid)
        if not r.evidence:
            iss.err(f, r.x_row, oid, "evidence empty (rule §1-2: URL/query + quote)")
        if not r.source:
            iss.warn(f, r.x_row, oid, "source empty")
        if not r.owner:
            iss.warn(f, r.x_row, oid, "owner empty")
        try:
            est = parse_bool(r.isEstimate)
        except ValueError as e:
            iss.err(f, r.x_row, oid, f"isEstimate: {e}")
            est = False
        ests.append(est)
    df = df.copy()
    df["x_tid"] = tids
    df["x_isEstimate"] = ests
    return df


def _boundary_lookup(ref: Ref, tid: int, ex: pd.Timestamp):
    """(id, exDate) → rc2 split 경계 행(validFrom 일치 우선, 그다음 캘린더 정렬 exDate 일치). 없으면 None."""
    b = ref.split_boundaries
    g = b[b.tradingItemId == tid]
    if not len(g):
        return None
    m = g[g.validFrom == ex]
    if len(m):
        return m.iloc[0]
    m = g[g.exDate == ex]
    return m.iloc[0] if len(m) else None


def validate_split(df: pd.DataFrame, ref: Ref, iss: Issues, targets: dict) -> None:
    f = df["x_file"].iloc[0]
    closes = None
    if getattr(ref, "check_prices", True) and df.action.str.strip().isin(["drop", "set_factor", "move_date", "add"]).any():
        try:      # Y2a: one RawCloses for every id of the file (a per-row build would rescan base_data)
            closes = ref.closes_for(set(int(t) for t in df.x_tid.dropna()))
        except Exception as e:      # noqa: BLE001
            iss.warn(f, 0, "", f"raw price check unavailable for this file: {e}")
    for r in df.itertuples(index=False):
        oid, tid = r.overrideId, r.x_tid
        n_err_row = iss.n_err
        act = r.action.strip()
        if act not in SPLIT_ACTIONS:
            iss.err(f, r.x_row, oid, f"action '{act}' not in {SPLIT_ACTIONS}")
            continue
        try:
            ex = parse_date(r.exDate)
        except ValueError as e:
            iss.err(f, r.x_row, oid, f"exDate: {e}")
            continue
        if ex is None:
            iss.err(f, r.x_row, oid, "exDate empty")
            continue
        nf = nx = None
        try:
            nf = parse_float(r.newFactor)
        except ValueError as e:
            iss.err(f, r.x_row, oid, f"newFactor: {e}")
        try:
            nx = parse_date(r.newExDate)
        except ValueError as e:
            iss.err(f, r.x_row, oid, f"newExDate: {e}")
        if act in ("set_factor", "add"):
            if nf is None or not np.isfinite(nf) or nf <= 0:
                iss.err(f, r.x_row, oid, f"{act} needs newFactor > 0 (new shares per old share; 1:5 reverse = 0.2)")
            elif nf == 1.0:
                iss.err(f, r.x_row, oid, f"{act} newFactor 1.0 means no split (use drop)")
        if act == "move_date":
            if nx is None:
                iss.err(f, r.x_row, oid, "move_date needs newExDate")
            elif nx == ex:
                iss.err(f, r.x_row, oid, "move_date newExDate equals exDate")
        if act in ("drop", "confirm", "move_date") and nf is not None and nf != 1.0 and act != "confirm":
            iss.warn(f, r.x_row, oid, f"newFactor ignored for {act}")
        if tid is None:
            continue
        b = _boundary_lookup(ref, tid, ex)
        if act == "add":
            if b is not None:
                iss.err(f, r.x_row, oid, f"add: a split boundary already exists at {ex.date()} (factor {b.factorPrev:g}->{b.factorNext:g}); use set_factor/confirm")
            if nx is not None:
                iss.warn(f, r.x_row, oid, "add: newExDate ignored (exDate is the effective date)")
        else:
            if b is None:
                iss.err(f, r.x_row, oid, f"no rc2 split boundary for id {tid} at {ex.date()} (factors/split validFrom or ledger split exDate)")
            elif act == "move_date" and nx is not None:
                g = ref.split_boundaries[ref.split_boundaries.tradingItemId == tid].sort_values("validFrom")
                others = g[g.validFrom != b.validFrom]
                prev_b = others[others.validFrom < b.validFrom].validFrom.max() if len(others) else pd.NaT
                next_b = others[others.validFrom > b.validFrom].validFrom.min() if len(others) else pd.NaT
                if (pd.notna(prev_b) and nx <= prev_b) or (pd.notna(next_b) and nx >= next_b):
                    iss.err(f, r.x_row, oid, f"move_date: newExDate {nx.date()} crosses a neighbouring boundary (prev {prev_b}, next {next_b})")
        key = ("split", tid, ex)
        if key in targets:
            iss.err(f, r.x_row, oid, f"duplicate split target (id {tid}, exDate {ex.date()}) also in {targets[key]}")
        else:
            targets[key] = f"{file_label(f)}:{r.x_row} ({oid})"
        basis = str(getattr(r, "checkBasis", "") or "").strip()
        if basis and basis not in CHECK_BASIS:
            iss.err(f, r.x_row, oid, f"checkBasis '{basis}' not in {CHECK_BASIS}")
            basis = ""
        if act in ("drop", "set_factor", "move_date", "add") and iss.n_err == n_err_row and closes is not None:
            # Y2a: the same tri-state raw-jump check the applier runs (rc2 factors + rc2 raw closes). fail with checkBasis raw_jump/none/empty
            # = a correction contradicted by the prices without an independent basis -> error; fail with mi_ratio/document -> warning (Y2b evidence)
            try:
                g = ref.split[ref.split.tradingItemId == tid]
                rec = dict(oid=oid, tid=tid, exDate=ex, action=act, newFactor=nf, newExDate=nx, caseId=r.caseId, estimate=bool(r.x_isEstimate))
                g2, n_ch, why = _split_one(g, rec, ref.calendar)
                if not why.startswith("reject") and n_ch > 0:
                    state, detail = split_override_check(rec, g, g2, closes, ref.calendar)
                    targets.setdefault(("split_check", oid), (state, detail, basis))
                    if state == "fail" and tid in ref.pending_price_ids:
                        # the same run also corrects this id's prices (unit fixes): the rc2 prices used here are not the ones apply will check against
                        iss.warn(f, r.x_row, oid, f"raw price check fails on rc2 prices ({detail}) but a price override of this run touches id {tid}: deferred to apply (log status decides)")
                    elif state == "fail":
                        if basis in ("", "raw_jump", "none"):
                            iss.err(f, r.x_row, oid, f"raw price check FAILS ({detail}) and checkBasis is '{basis or 'empty'}': give an independent basis (checkBasis mi_ratio/document + evidence) or fix the correction")
                        else:
                            iss.warn(f, r.x_row, oid, f"raw price check fails ({detail}); accepted on checkBasis={basis} (apply logs applied_unverified)")
                    elif state == "uncheckable":
                        iss.warn(f, r.x_row, oid, f"raw price check not possible ({detail}); apply logs applied_unchecked" + (f" (checkBasis={basis})" if basis else ""))
            except Exception as e:      # noqa: BLE001 — the check must never mask the schema validation
                iss.warn(f, r.x_row, oid, f"raw price check skipped: {e}")
        targets.setdefault(("any", tid, ex), []).append(f"split:{oid}")


def validate_price(df: pd.DataFrame, ref: Ref, iss: Issues, targets: dict, ranges: list) -> None:
    f = df["x_file"].iloc[0]
    for r in df.itertuples(index=False):
        oid, tid = r.overrideId, r.x_tid
        try:
            d0, d1 = parse_date(r.dateFrom), parse_date(r.dateTo)
        except ValueError as e:
            iss.err(f, r.x_row, oid, f"dateFrom/dateTo: {e}")
            continue
        if d0 is None or d1 is None:
            iss.err(f, r.x_row, oid, "dateFrom and dateTo required")
            continue
        if d1 < d0:
            iss.err(f, r.x_row, oid, f"dateTo {d1.date()} < dateFrom {d0.date()}")
            continue
        op = r.op.strip()
        if op not in PRICE_OPS:
            iss.err(f, r.x_row, oid, f"op '{op}' not in {PRICE_OPS}")
            continue
        try:
            val = parse_float(r.value)
        except ValueError as e:
            iss.err(f, r.x_row, oid, f"value: {e}")
            continue
        if val is None or not np.isfinite(val) or val <= 0:
            iss.err(f, r.x_row, oid, "value must be a finite number > 0")
            continue
        if op == "multiply" and val == 1.0:
            iss.err(f, r.x_row, oid, "multiply by 1.0 is a no-op")
        fields = tuple(x.strip() for x in (r.fields or "").replace(";", ",").split(",") if x.strip()) or PRICE_FIELDS
        bad = [x for x in fields if x not in PRICE_FIELDS]
        if bad:
            iss.err(f, r.x_row, oid, f"fields {bad} not in {PRICE_FIELDS} (volume is never touched)")
            continue
        if op == "set_close" and fields != ("close",) and set(fields) != {"close"}:
            iss.warn(f, r.x_row, oid, f"set_close applies the same value to fields {fields}")
        if tid is None:
            continue
        pr = ref.price_range
        if tid in pr.index:
            first, last = pr.loc[tid, "first"], pr.loc[tid, "last"]
            if (pd.notna(last) and d0 > last) or (pd.notna(first) and d1 < first):
                iss.err(f, r.x_row, oid, f"date range {d0.date()}..{d1.date()} outside the id's price range {first.date() if pd.notna(first) else None}..{last.date() if pd.notna(last) else None}")
            elif (pd.notna(last) and d1 > last) or (pd.notna(first) and d0 < first):
                iss.warn(f, r.x_row, oid, "date range partly outside the id's price range (only existing rows are changed)")
        for (t2, a, b, fl, who) in ranges:
            if t2 == tid and not (d1 < a or d0 > b) and set(fl) & set(fields):
                iss.err(f, r.x_row, oid, f"overlapping price override for id {tid} with {who}")
        ranges.append((tid, d0, d1, fields, f"{file_label(f)}:{r.x_row} ({oid})"))
        targets.setdefault(("any", tid, d0), []).append(f"price:{oid}")


def _resolve_ledger_target(r, ref: Ref):
    """(target_rows DataFrame, error str). eventId 우선, 없으면 match_*."""
    L = ref.ledger
    if r.eventId:
        m = L[L.eventId == r.eventId]
        if not len(m):
            return None, f"eventId '{r.eventId}' not in rc2 ledger"
        return m, ""
    try:
        mt = parse_int(r.match_tradingItemId)
        mx = parse_date(r.match_exDate)
        mc = parse_int(r.match_childTradingItemId)
    except ValueError as e:
        return None, f"match_*: {e}"
    ml = (r.match_legType or "").strip()
    if mt is None or mx is None or not ml:
        return None, "set/drop needs eventId or match_tradingItemId+match_exDate+match_legType"
    if ml not in LEG_TYPES:
        return None, f"match_legType '{ml}' not in {LEG_TYPES}"
    m = L[(L.tradingItemId == mt) & (L.exDate == mx) & (L.legType == ml)]
    if mc is not None:
        m = m[m.childTradingItemId.astype("Int64") == mc]
    if not len(m):
        return None, f"no rc2 ledger row matches ({mt}, {mx.date()}, {ml}, child {mc})"
    if len(m) > 1:
        return None, f"{len(m)} rc2 ledger rows match ({mt}, {mx.date()}, {ml}, child {mc}): {list(m.eventId)} — use eventId"
    return m, ""


def _check_ledger_set_values(setd: dict, f, row, oid, iss: Issues, ref: Ref, action: str) -> dict:
    """set JSON 값 검사·정규화(날짜 Timestamp, 숫자 float, bool). 반환 정규화 dict."""
    out = {}
    for k, v in setd.items():
        if k not in LEDGER_SET_KEYS:
            iss.err(f, row, oid, f"set key '{k}' not allowed (allowed {LEDGER_SET_KEYS})")
            continue
        if k in LEDGER_DATE_KEYS:
            try:
                d = parse_date(str(v)) if v is not None else None
            except ValueError as e:
                iss.err(f, row, oid, f"set.{k}: {e}")
                continue
            if d is None:
                out[k] = pd.NaT
            else:
                out[k] = d
                if k == "exDate":
                    al, moved = ref.calendar.align(np.array([d.to_datetime64()]))
                    if moved[0]:
                        iss.warn(f, row, oid, f"set.exDate {d.date()} is not a trading day (ledger_build aligns to {pd.Timestamp(al[0]).date()})")
        elif k in LEDGER_NUM_KEYS:
            if v is None or (isinstance(v, str) and v.strip().lower() in ("", "nan", "null")):
                out[k] = np.nan
                continue
            try:
                x = float(v)
            except (TypeError, ValueError):
                iss.err(f, row, oid, f"set.{k} '{v}' not numeric")
                continue
            if k in ("childRatio", "parentFactor", "exerciseRatio") and np.isfinite(x) and x <= 0:
                iss.err(f, row, oid, f"set.{k} must be > 0")
            if k in ("cashPerShare", "childValuePerOldShare", "subscriptionPrice") and np.isfinite(x) and x < 0:
                iss.err(f, row, oid, f"set.{k} must be >= 0")
            out[k] = x
        elif k == "childTradingItemId":
            if v is None or (isinstance(v, str) and v.strip() == ""):
                out[k] = None
                continue
            try:
                c = parse_int(str(v))
            except ValueError as e:
                iss.err(f, row, oid, f"set.childTradingItemId: {e}")
                continue
            if c not in ref.ids:
                if c in ref.pending_aux_ids:
                    iss.warn(f, row, oid, f"set.childTradingItemId {c} collected by aux_additions.csv in this run (valued by P10 ① once priced)")
                else:
                    iss.warn(f, row, oid, f"set.childTradingItemId {c} not in base_data/master: the leg cannot be valued by P10 ① (will be unresolved unless childValuePerOldShare is given)")
            out[k] = c
        elif k == "isEstimate":
            if isinstance(v, bool):
                out[k] = v
            else:
                try:
                    out[k] = parse_bool(str(v))
                except ValueError as e:
                    iss.err(f, row, oid, f"set.isEstimate: {e}")
        elif k == "eventType":
            if v not in EVENT_TYPES:
                iss.err(f, row, oid, f"set.eventType '{v}' not in {EVENT_TYPES}")
            out[k] = v
        elif k == "legType":
            if v not in LEG_TYPES:
                iss.err(f, row, oid, f"set.legType '{v}' not in {LEG_TYPES}")
            out[k] = v
        elif k == "status":
            if v not in STATUSES:
                iss.err(f, row, oid, f"set.status '{v}' not in {STATUSES}")
            out[k] = v
        elif k == "childValueSource":
            if v not in VALUE_SOURCES:
                iss.err(f, row, oid, f"set.childValueSource '{v}' not in {VALUE_SOURCES}")
            out[k] = v
        else:
            out[k] = "" if v is None else str(v)
    return out


def validate_ledger(df: pd.DataFrame, ref: Ref, iss: Issues, targets: dict) -> None:
    f = df["x_file"].iloc[0]
    for r in df.itertuples(index=False):
        oid, tid = r.overrideId, r.x_tid
        act = r.action.strip()
        if act not in LEDGER_ACTIONS:
            iss.err(f, r.x_row, oid, f"action '{act}' not in {LEDGER_ACTIONS}")
            continue
        setd = {}
        if r.set.strip():
            try:
                setd = json.loads(r.set)
            except json.JSONDecodeError as e:
                iss.err(f, r.x_row, oid, f"set is not valid JSON: {e}")
                continue
            if not isinstance(setd, dict):
                iss.err(f, r.x_row, oid, "set JSON must be an object")
                continue
        if act in ("set", "add") and not setd:
            iss.err(f, r.x_row, oid, f"{act} needs a non-empty set JSON")
            continue
        vals = _check_ledger_set_values(setd, f, r.x_row, oid, iss, ref, act)
        if act in ("set", "drop"):
            m, err = _resolve_ledger_target(r, ref)
            if err:
                iss.err(f, r.x_row, oid, err)
                continue
            row = m.iloc[0]
            if str(row.source) == "stitched_copy":
                keeper = ref.ledger[ref.ledger.eventId == str(row.keeperEventId)]
                if not len(keeper):
                    iss.err(f, r.x_row, oid, f"target {row.eventId} is a stitched_copy without a keeper row; nothing to correct")
                    continue
                iss.warn(f, r.x_row, oid, f"target {row.eventId} is a stitched_copy (exportExclude): the correction is applied to the keeper {row.keeperEventId} and the copy is regenerated from it")
                row = keeper.iloc[0]
                tid = int(row.tradingItemId) if tid is None or tid != int(row.tradingItemId) else tid
            if tid is not None and int(row.tradingItemId) != tid:
                iss.err(f, r.x_row, oid, f"tradingItemId {tid} != target row's parent {int(row.tradingItemId)} ({row.eventId})")
            et = vals.get("eventType", row.eventType)
            lt = vals.get("legType", row.legType)
            if et in LEG_OF_TYPE and lt not in LEG_OF_TYPE[et]:
                iss.err(f, r.x_row, oid, f"eventType {et} with legType {lt} (allowed {LEG_OF_TYPE[et]})")
            if act == "set" and any(k in vals for k in ("exDate", "legType", "childTradingItemId", "eventType")):
                new_ex = vals.get("exDate", pd.Timestamp(row.exDate)); new_lt = vals.get("legType", row.legType); new_et = vals.get("eventType", row.eventType)
                new_child = vals.get("childTradingItemId", None if pd.isna(row.childTradingItemId) else int(row.childTradingItemId)) if "childTradingItemId" in vals else (None if pd.isna(row.childTradingItemId) else int(row.childTradingItemId))
                if pd.notna(new_ex):
                    L = ref.ledger
                    clash = L[(L.tradingItemId == int(row.tradingItemId)) & (L.exDate == pd.Timestamp(new_ex)) & (L.legType == new_lt) & (L.eventId != row.eventId)
                              & (L.eventType.isin(MERGER_TYPES) == (new_et in MERGER_TYPES)) & (L.source != "stitched_copy")]
                    clash = clash[clash.childTradingItemId.isna()] if new_child is None else clash[clash.childTradingItemId.astype("Int64") == new_child]
                    if len(clash):
                        iss.err(f, r.x_row, oid, f"set result key (id {int(row.tradingItemId)}, {pd.Timestamp(new_ex).date()}, {new_lt}, child {new_child}) collides with existing {list(clash.eventId)} "
                                                 f"(status {list(clash.status)}): double count — drop/set that row instead")
            if act == "set":
                if "status" in vals and vals["status"] == "estimated" and vals.get("isEstimate", True) is False:
                    iss.err(f, r.x_row, oid, "status estimated with isEstimate false")
                if r.x_isEstimate and vals.get("status") == "ok":
                    iss.err(f, r.x_row, oid, "csv isEstimate=true but set.status=ok")
                if lt in ("stock", "rights") and "childRatio" in vals and vals.get("childTradingItemId", row.childTradingItemId) is None and pd.isna(row.childTradingItemId) \
                        and "childValuePerOldShare" not in vals:
                    iss.warn(f, r.x_row, oid, "childRatio set without a child id or a value: stays unresolved")
            key = ("ledger", row.eventId)
            if key in targets:
                iss.err(f, r.x_row, oid, f"duplicate ledger target {row.eventId} also in {targets[key]}")
            else:
                targets[key] = f"{file_label(f)}:{r.x_row} ({oid})"
            targets.setdefault(("any", int(row.tradingItemId), pd.Timestamp(row.exDate)), []).append(f"ledger:{oid}")
            if act == "set" and lt == "cash" and et not in MERGER_TYPES:      # X3 follow-up: csv dividend cash vs lifecycle settlement cash (double count check)
                cash_v = vals.get("cashPerShare", float(row.cashPerShare) if pd.notna(row.cashPerShare) else np.nan)
                targets.setdefault(("ledger_cash", int(row.tradingItemId)), []).append((pd.Timestamp(vals.get("exDate", row.exDate)), cash_v, oid, et, f, r.x_row))
        else:   # add
            need = ("eventType", "legType", "exDate")
            miss = [k for k in need if k not in vals]
            if miss:
                iss.err(f, r.x_row, oid, f"add needs set keys {need}; missing {miss}")
                continue
            if r.eventId:
                iss.err(f, r.x_row, oid, "add must not carry an eventId (new row)")
            et, lt = vals["eventType"], vals["legType"]
            if et in LEG_OF_TYPE and lt not in LEG_OF_TYPE[et]:
                iss.err(f, r.x_row, oid, f"eventType {et} with legType {lt} (allowed {LEG_OF_TYPE[et]})")
            if et in ("rename", "relist"):
                iss.err(f, r.x_row, oid, "rename/relist rows come from lineage_overrides, not ledger add")
            if et in ("split", "stock_dividend"):
                iss.err(f, r.x_row, oid, "split/stock_dividend rows come from split_overrides (factors boundary), not ledger add")
            st = vals.get("status", "estimated" if r.x_isEstimate else "ok")
            if lt == "cash" and st != "unresolved" and not np.isfinite(vals.get("cashPerShare", np.nan)):
                iss.err(f, r.x_row, oid, "add cash leg needs cashPerShare (pre-tax USD per old share) unless status unresolved")
            if lt in ("stock", "rights") and st != "unresolved":
                if vals.get("childTradingItemId") is None and not np.isfinite(vals.get("childValuePerOldShare", np.nan)):
                    iss.err(f, r.x_row, oid, "add stock/rights leg needs childTradingItemId (+childRatio) or childValuePerOldShare unless status unresolved")
                if vals.get("childTradingItemId") is not None and not np.isfinite(vals.get("childRatio", np.nan)):
                    iss.err(f, r.x_row, oid, "add stock/rights leg with a child id needs childRatio (child shares per old share)")
            if lt == "quantity" and not np.isfinite(vals.get("parentFactor", np.nan)):
                iss.err(f, r.x_row, oid, "add quantity leg needs parentFactor")
            if st == "estimated" and vals.get("isEstimate", True) is False:
                iss.err(f, r.x_row, oid, "status estimated with isEstimate false")
            if tid is None or pd.isna(vals.get("exDate", pd.NaT)):
                continue
            ex = vals["exDate"]
            child = vals.get("childTradingItemId")
            L = ref.ledger
            m = L[(L.tradingItemId == tid) & (L.exDate == ex) & (L.legType == lt) & (L.eventType.isin(MERGER_TYPES) == (et in MERGER_TYPES))]
            m = m[m.childTradingItemId.isna()] if child is None else m[m.childTradingItemId.astype("Int64") == child]
            if len(m):
                iss.err(f, r.x_row, oid, f"add: rc2 ledger already has {list(m.eventId)} with this key (id, exDate, legType, child); use set")
            key = ("ledger_add", tid, ex, lt, child, et in MERGER_TYPES)
            if key in targets:
                iss.err(f, r.x_row, oid, f"duplicate ledger add for the same key also in {targets[key]}")
            else:
                targets[key] = f"{file_label(f)}:{r.x_row} ({oid})"
            targets.setdefault(("any", tid, ex), []).append(f"ledger:{oid}")
            if lt == "cash" and et not in MERGER_TYPES:
                targets.setdefault(("ledger_cash", tid), []).append((ex, vals.get("cashPerShare", np.nan), oid, et, f, r.x_row))


def validate_lifecycle(df: pd.DataFrame, ref: Ref, iss: Issues, targets: dict) -> None:
    f = df["x_file"].iloc[0]
    lc = ref.lifecycle
    for r in df.itertuples(index=False):
        oid, tid = r.overrideId, r.x_tid
        dates = {}
        ok = True
        for c in ("lastTradeDate", "effectiveDate", "settlementDate", "payDate"):
            try:
                dates[c] = parse_date(getattr(r, c))
            except ValueError as e:
                iss.err(f, r.x_row, oid, f"{c}: {e}")
                ok = False
        if not ok:
            continue
        er, tr = r.endReason.strip(), r.treatment.strip()
        if er not in END_REASONS:
            iss.err(f, r.x_row, oid, f"endReason '{er}' not in {END_REASONS}")
        if tr not in TREATMENTS:
            iss.err(f, r.x_row, oid, f"treatment '{tr}' not in {TREATMENTS}")
            continue
        try:
            succ = parse_int(r.successorTradingItemId)
            ratio = parse_float(r.successorRatio)
            cash = parse_float(r.cashPerShare)
        except ValueError as e:
            iss.err(f, r.x_row, oid, f"successor/ratio/cash: {e}")
            continue
        if succ is not None and succ not in ref.ids:
            if succ in ref.pending_aux_ids:
                iss.warn(f, r.x_row, oid, f"successorTradingItemId {succ} not in base_data/master yet: collected by aux_additions.csv in this run")
            else:
                iss.err(f, r.x_row, oid, f"successorTradingItemId {succ} not in base_data/master (collect it as aux first)")
        if ratio is not None and (not np.isfinite(ratio) or ratio <= 0):
            iss.err(f, r.x_row, oid, "successorRatio must be > 0")
        if cash is not None and (not np.isfinite(cash) or cash < 0):
            iss.err(f, r.x_row, oid, "cashPerShare must be >= 0")
        if tr in ("settle_cash", "settle_mixed") and cash is None:
            iss.err(f, r.x_row, oid, f"{tr} needs cashPerShare")
        if tr in ("convert_successor", "settle_mixed") and (succ is None or ratio is None):
            iss.err(f, r.x_row, oid, f"{tr} needs successorTradingItemId and successorRatio")
        elif succ is not None and ratio is None:      # R-X4 re-review: a successor without a ratio would reach ratio x close in the events patch (extend_prices stage 2 included)
            iss.err(f, r.x_row, oid, f"successorTradingItemId {succ} given without successorRatio: the ratio is required whenever a successor is given "
                                     f"({'extend_prices applies the successor terms at the new lastTradeDate' if tr == 'extend_prices' else 'treatment ' + tr})")
        if succ is None and ratio is not None and tr != "last_close":
            iss.warn(f, r.x_row, oid, "successorRatio given without successorTradingItemId (ignored)")
        # R-X4 §4-9: successorRatio must agree with the lineage ratio of the same (old, new) pair — a lineage override of this run first, else the rc2 lineage row
        if succ is not None and ratio is not None and tid is not None:
            pr = ref.pending_lineage_ratios.get((tid, succ))
            if pr is not None:
                if abs(pr[0] - ratio) > 1e-4 * max(1.0, abs(pr[0])):
                    iss.err(f, r.x_row, oid, f"successorRatio {ratio:g} conflicts with lineage override {pr[1]} ratio {pr[0]:g} for ({tid} -> {succ})")
            else:
                ln = ref.lineage
                m = ln[(ln.oldId == tid) & (ln.newId == succ)]
                if len(m) and pd.notna(m.ratio.iloc[0]) and abs(float(m.ratio.iloc[0]) - ratio) > 1e-4 * max(1.0, abs(float(m.ratio.iloc[0]))):
                    iss.err(f, r.x_row, oid, f"successorRatio {ratio:g} conflicts with rc2 lineage ratio {float(m.ratio.iloc[0]):g} ({m.relation.iloc[0]}, {m.ratioSource.iloc[0] if 'ratioSource' in m else ''}) for ({tid} -> {succ}); add a lineage_overrides set_ratio or fix one side")
        if tr in ("settle_cash", "convert_successor", "settle_mixed") and dates["settlementDate"] is None and dates["payDate"] is None and dates["effectiveDate"] is None:
            iss.err(f, r.x_row, oid, f"{tr} needs a settlement/pay/effective date (the engine settles on that bar)")
        if tr == "last_close" and (succ is not None or cash is not None):
            iss.warn(f, r.x_row, oid, "last_close ignores successor/cash terms")
        if tr == "unresolved" and dates["settlementDate"] is not None:
            iss.warn(f, r.x_row, oid, "unresolved with a settlementDate (brief: halted lines keep settlementDate empty)")
        if tid is None:
            continue
        if tid not in lc.index:
            iss.err(f, r.x_row, oid, f"id {tid} not in lifecycle")
            continue
        cur_last = pd.to_datetime(lc.at[tid, "lastTradeDate"])
        if tr == "extend_prices":
            if dates["lastTradeDate"] is None:
                iss.err(f, r.x_row, oid, "extend_prices needs lastTradeDate")
            elif pd.notna(cur_last) and dates["lastTradeDate"] <= cur_last:
                iss.err(f, r.x_row, oid, f"extend_prices lastTradeDate {dates['lastTradeDate'].date()} not after current {cur_last.date()}")
            pr = ref.price_range
            if dates["lastTradeDate"] is not None and tid in pr.index and pd.notna(pr.loc[tid, "last"]) and dates["lastTradeDate"] > pr.loc[tid, "last"]:
                iss.err(f, r.x_row, oid, f"extend_prices beyond the last vendor close {pr.loc[tid, 'last'].date()} (no price rows to extend into)")
        if dates["lastTradeDate"] is not None and tr != "extend_prices" and pd.notna(cur_last) and dates["lastTradeDate"] > cur_last:
            iss.warn(f, r.x_row, oid, f"lastTradeDate {dates['lastTradeDate'].date()} later than lifecycle {cur_last.date()} without treatment extend_prices")
        if dates["lastTradeDate"] is not None:
            al, moved = ref.calendar.align(np.array([dates["lastTradeDate"].to_datetime64()]))
            if moved[0]:
                iss.err(f, r.x_row, oid, f"lastTradeDate {dates['lastTradeDate'].date()} is not a trading day")
        lt_eff = dates["lastTradeDate"] if dates["lastTradeDate"] is not None else cur_last
        for c in ("settlementDate", "payDate", "effectiveDate"):
            if dates[c] is not None and pd.notna(lt_eff) and dates[c] < lt_eff:
                iss.warn(f, r.x_row, oid, f"{c} {dates[c].date()} before lastTradeDate {lt_eff.date()}")
        if tid not in ref.lifecycle_dates.index and tr != "extend_prices":
            iss.warn(f, r.x_row, oid, f"id {tid} has no lifecycle_dates/events row (still trading in rc2): apply adds an events row only for settle_*/convert_successor")
        key = ("lifecycle", tid)
        if key in targets:
            iss.err(f, r.x_row, oid, f"duplicate lifecycle override for id {tid} also in {targets[key]}")
        else:
            targets[key] = f"{file_label(f)}:{r.x_row} ({oid})"
        anchor = dates["lastTradeDate"] or dates["settlementDate"] or dates["effectiveDate"] or cur_last
        if pd.notna(anchor):
            targets.setdefault(("any", tid, pd.Timestamp(anchor)), []).append(f"lifecycle:{oid}")
        if tr in ("settle_cash", "settle_mixed") and cash is not None and np.isfinite(cash):
            sd = dates["settlementDate"] or dates["payDate"] or dates["effectiveDate"] or dates["lastTradeDate"] or cur_last
            targets.setdefault(("lc_cash", tid), []).append((pd.Timestamp(sd) if pd.notna(sd) else pd.NaT, float(cash), oid, tr, f, r.x_row))
            # rc2 ledger already carries a (non-merger) dividend cash row within ±5 days of the settlement -> the engine would pay both
            L = ref.ledger
            if pd.notna(sd):
                near = L[(L.tradingItemId == tid) & (L.legType == "cash") & (~L.eventType.isin(MERGER_TYPES)) & (L.source != "stitched_copy")
                         & ((L.exDate - pd.Timestamp(sd)).abs().dt.days <= 5)]
                lt_ref = pd.to_datetime(lc.at[tid, "lastTradeDate"]) if tid in lc.index else pd.NaT
                lt_eff = dates["lastTradeDate"] if dates["lastTradeDate"] is not None else lt_ref
                for e in near.itertuples(index=False):
                    # R-X0 2nd review P2-14: a ledger cash row after the id's lastTradeDate is paid by neither returns v2 (row masked) nor the export
                    # (cash_leg_after_parent_lastTradeDate) -> only rows on/before lastTradeDate are a double-payment risk
                    paid = pd.notna(lt_eff) and pd.Timestamp(e.exDate) <= pd.Timestamp(lt_eff)
                    tail = ("returns v2 pays it on its ex bar: check the settlement cash is the residual only" if paid else
                            f"after lastTradeDate {pd.Timestamp(lt_eff).date() if pd.notna(lt_eff) else '?'}: neither returns v2 nor the engine export pays it, "
                            f"no double payment; include it in the settlement cash if it is part of the consideration")
                    iss.warn(f, r.x_row, oid, f"{tr} cash {cash:g} on {pd.Timestamp(sd).date()} while the rc2 ledger has {e.eventType} cash {e.cashPerShare:g} on "
                                              f"{pd.Timestamp(e.exDate).date()} ({e.eventId}; {tail})")


def validate_lineage(df: pd.DataFrame, ref: Ref, iss: Issues, targets: dict) -> None:
    f = df["x_file"].iloc[0]
    ln = ref.lineage
    pairs = set(zip(ln.oldId.astype(int), ln.newId.astype(int)))
    for r in df.itertuples(index=False):
        oid = r.overrideId
        try:
            old, new = parse_int(r.oldId), parse_int(r.newId)
            ratio = parse_float(r.ratio)
            eff = parse_date(r.effectiveDate)
        except ValueError as e:
            iss.err(f, r.x_row, oid, f"oldId/newId/ratio/effectiveDate: {e}")
            continue
        if old is None or new is None:
            iss.err(f, r.x_row, oid, "oldId and newId required")
            continue
        for nm, v in (("oldId", old), ("newId", new)):
            if v not in ref.ids:
                if v in ref.pending_aux_ids:
                    iss.warn(f, r.x_row, oid, f"{nm} {v} collected by aux_additions.csv in this run")
                else:
                    iss.err(f, r.x_row, oid, f"{nm} {v} not in base_data/master")
        if r.x_tid is not None and r.x_tid not in (old, new):
            iss.warn(f, r.x_row, oid, f"tradingItemId {r.x_tid} is neither oldId nor newId")
        act = r.action.strip()
        if act not in LINEAGE_ACTIONS:
            iss.err(f, r.x_row, oid, f"action '{act}' not in {LINEAGE_ACTIONS}")
            continue
        exists = (old, new) in pairs
        if act in ("set_ratio", "add") and (ratio is None or not np.isfinite(ratio) or ratio <= 0):
            iss.err(f, r.x_row, oid, f"{act} needs ratio > 0 (new shares per old share)")
        if act == "add":
            if exists:
                iss.err(f, r.x_row, oid, f"add: lineage pair ({old}, {new}) already exists; use set_ratio/set_chainable")
            if eff is None:
                iss.err(f, r.x_row, oid, "add needs effectiveDate")
            rel = r.relation.strip()
            if rel not in LINEAGE_RELATIONS or rel == "unknown":
                iss.err(f, r.x_row, oid, f"add needs relation in {LINEAGE_RELATIONS[:-1]}")
            if (old, new) in {(b, a) for a, b in pairs}:
                iss.err(f, r.x_row, oid, f"add: reversed pair ({new}, {old}) exists")
        elif not exists:
            iss.err(f, r.x_row, oid, f"{act}: lineage pair ({old}, {new}) not in rc2 lineage")
        if act == "set_chainable":
            try:
                parse_bool(r.chainable)
            except ValueError as e:
                iss.err(f, r.x_row, oid, f"chainable: {e}")
        lv_txt = str(getattr(r, "linkVerified", "") or "").strip()       # rc2f-E optional column
        if lv_txt:
            try:
                lv = parse_bool(lv_txt)
            except ValueError as e:
                iss.err(f, r.x_row, oid, f"linkVerified: {e}")
            else:
                if lv and not str(r.evidence).strip():
                    iss.err(f, r.x_row, oid, "linkVerified true needs evidence (why the first-day move is a genuine market move: volume, MI match, no split, 1:1)")
                if lv and act == "drop":
                    iss.warn(f, r.x_row, oid, "linkVerified ignored for drop")
        if act == "drop" and exists:
            rel = ln[(ln.oldId == old) & (ln.newId == new)].relation.iloc[0]
            if rel in ("rename", "relist"):
                iss.warn(f, r.x_row, oid, f"drop of a {rel} link removes the returns first-row link and the ledger rename/relist row of {new}")
        if act in ("set_ratio", "set_chainable") and exists:      # R-X0 P1-4: the generator links only chainGapTradingDays <= MAX_CHAIN_GAP
            m0 = ln[(ln.oldId == old) & (ln.newId == new)]
            gap = m0.chainGapTradingDays.iloc[0] if "chainGapTradingDays" in m0.columns else np.nan
            if pd.notna(gap) and int(gap) > MAX_CHAIN_GAP and not (act == "set_chainable" and str(r.chainable).strip().lower() in ("false", "0", "no")):
                iss.warn(f, r.x_row, oid, f"pair ({old}, {new}) has chainGapTradingDays {int(gap)} > {MAX_CHAIN_GAP}: the returns generator will not link it "
                                          f"(apply logs applied_no_effect(chain_gap); the ledger rename/relist row still takes the ratio)")
        key = ("lineage", old, new)
        if key in targets:
            iss.err(f, r.x_row, oid, f"duplicate lineage target ({old}, {new}) also in {targets[key]}")
        else:
            targets[key] = f"{file_label(f)}:{r.x_row} ({oid})"
        if exists:
            effd = pd.to_datetime(ln[(ln.oldId == old) & (ln.newId == new)].effectiveDate.iloc[0])
        else:
            effd = eff
        if effd is not None and pd.notna(effd):
            targets.setdefault(("any", old, pd.Timestamp(effd)), []).append(f"lineage:{oid}")


def validate_value(df: pd.DataFrame, ref: Ref, iss: Issues, targets: dict, cells: list) -> None:
    """value_overrides: field 이름, 값 유한·양수(volume >= 0), 날짜 거래일, (id, date, field) 중복 금지, lifecycle 밖 날짜는 경고(행이 없으면 적용 시 거부).
    price_overrides 와 같은 셀 충돌은 validate_files 가 모든 파일을 본 뒤 cells 로 검사."""
    f = df["x_file"].iloc[0]
    days = ref.calendar.days
    lc = ref.lifecycle
    for r in df.itertuples(index=False):
        oid, tid = r.overrideId, r.x_tid
        try:
            d = parse_date(r.date)
        except ValueError as e:
            iss.err(f, r.x_row, oid, f"date: {e}")
            continue
        if d is None:
            iss.err(f, r.x_row, oid, "date required")
            continue
        field = str(r.field).strip()
        if field not in VALUE_FIELDS:
            iss.err(f, r.x_row, oid, f"field '{field}' not in {VALUE_FIELDS}")
            continue
        try:
            val = parse_float(r.value)
        except ValueError as e:
            iss.err(f, r.x_row, oid, f"value: {e}")
            continue
        if val is None or not np.isfinite(val) or (val < 0 if field == "volume" else val <= 0):
            iss.err(f, r.x_row, oid, "value must be finite and " + (">= 0 (volume)" if field == "volume" else "> 0"))
            continue
        if field != "volume" and not np.isfinite(np.float32(val)):
            iss.err(f, r.x_row, oid, f"value {val:g} is not finite as float32 (price fields are stored as float32)")
            continue
        d64 = np.datetime64(d, "D")
        i = int(np.searchsorted(days, d64))
        if i >= len(days) or days[i] != d64:
            iss.err(f, r.x_row, oid, f"date {d.date()} is not a trading day")
            continue
        if tid is None:
            continue
        key = ("value", tid, d, field)
        if key in targets:
            iss.err(f, r.x_row, oid, f"duplicate value override ({tid}, {d.date()}, {field}) also in {targets[key]}")
            continue
        targets[key] = f"{file_label(f)}:{r.x_row} ({oid})"
        if tid in lc.index:
            ft, lt = pd.to_datetime(lc.at[tid, "firstTradeDate"]), pd.to_datetime(lc.at[tid, "lastTradeDate"])
            if (pd.notna(ft) and d < ft) or (pd.notna(lt) and d > lt):
                iss.warn(f, r.x_row, oid, f"date {d.date()} outside lifecycle [{ft.date() if pd.notna(ft) else None}, {lt.date() if pd.notna(lt) else None}]: applied only to an existing base_data row (no new row)")
        cells.append((tid, d, field, f, r.x_row, oid))


def validate_aux(df: pd.DataFrame, ref: Ref, iss: Issues, targets: dict) -> None:
    f = df["x_file"].iloc[0]
    for r in df.itertuples(index=False):
        if r.x_tid is None:
            continue
        key = ("aux", int(r.x_tid))
        if key in targets:
            iss.warn(f, r.x_row, r.overrideId, f"aux addition for id {r.x_tid} also in {targets[key]} (allowed: second one is a no-op)")
        else:
            targets[key] = f"{file_label(f)}:{r.x_row} ({r.overrideId})"


def validate_files(paths: list, ref: Ref) -> tuple[list, Issues]:
    """반환 (파일별 요약 행, Issues). 파일 간 overrideId 유일·같은 대상 중복도 검사. 같은 실행의 aux_additions.csv 에 있는 id 는
    승계사/자회사/계보 id 로 허용(경고): 적용 순서가 aux 수집 → 나머지이므로 적용 시점에는 base_data(aux) 에 있다."""
    iss = Issues()
    summary = []
    targets: dict = {}
    ranges: list = []
    vcells: list = []
    seen_ids: dict = {}
    for p in paths:
        if kind_of(p) == "aux" and Path(p).exists():
            try:
                a = read_override_csv(p)
                if "tradingItemId" in a.columns:
                    ref.pending_aux_ids |= {int(float(x)) for x in a.tradingItemId if str(x).strip()}
            except Exception:      # noqa: BLE001 (reported below when the file itself is validated)
                pass
        if kind_of(p) == "lineage" and Path(p).exists():
            try:
                a = read_override_csv(p)
                for r in a.itertuples(index=False):
                    if str(r.action).strip() in ("set_ratio", "add") and str(r.ratio).strip() and str(r.oldId).strip() and str(r.newId).strip():
                        ref.pending_lineage_ratios[(int(float(r.oldId)), int(float(r.newId)))] = (float(r.ratio), r.overrideId)
            except Exception:      # noqa: BLE001
                pass
        if kind_of(p) == "price" and Path(p).exists():
            try:
                a = read_override_csv(p)
                ref.pending_price_ids |= {int(float(x)) for x in a.tradingItemId if str(x).strip()}
            except Exception:      # noqa: BLE001
                pass
    for p in paths:
        p = Path(p)
        kind = kind_of(p)
        if kind is None:
            iss.err(str(p), 0, "", f"cannot tell the override kind from the file name (expected {', '.join(k + '_*.csv' for k in KINDS)})")
            summary.append({"file": file_label(p), "kind": "?", "rows": 0})
            continue
        if not p.exists():
            iss.err(str(p), 0, "", "file not found")
            summary.append({"file": file_label(p), "kind": kind, "rows": 0})
            continue
        try:
            df = read_override_csv(p)
        except Exception as e:      # noqa: BLE001
            iss.err(str(p), 0, "", f"cannot read csv: {e}")
            summary.append({"file": file_label(p), "kind": kind, "rows": 0})
            continue
        n0 = iss.n_err
        if len(df) == 0:
            summary.append({"file": file_label(p), "kind": kind, "rows": 0, "errors": 0, "warnings": 0})
            continue
        df = _common_checks(df, kind, ref, iss)
        if df is not None:
            for r in df.itertuples(index=False):
                if r.overrideId:
                    if r.overrideId in seen_ids:
                        iss.err(str(p), r.x_row, r.overrideId, f"overrideId not unique (also {seen_ids[r.overrideId]})")
                    else:
                        seen_ids[r.overrideId] = f"{file_label(p)}:{r.x_row}"
            {"split": validate_split, "price": lambda d, rf, i, t: validate_price(d, rf, i, t, ranges), "ledger": validate_ledger,
             "lifecycle": validate_lifecycle, "lineage": validate_lineage, "aux": validate_aux,
             "value": lambda d, rf, i, t: validate_value(d, rf, i, t, vcells)}[kind](df, ref, iss, targets)
        n_err = iss.n_err - n0
        summary.append({"file": file_label(p), "kind": kind, "rows": int(len(df)), "errors": n_err,
                        "warnings": sum(1 for x in iss.rows if x["level"] == "WARN" and x["file"] == file_label(p))})
    # value_overrides 셀이 price_overrides 범위·필드와 겹치면 오류(파일 순서와 무관하게 모든 파일을 본 뒤)
    for (tid, d, field, vf, vrow, void) in vcells:
        for (t2, a, b, fl, who) in ranges:
            if t2 == tid and a <= d <= b and field in fl:
                iss.err(vf, vrow, void, f"value override ({tid}, {d.date()}, {field}) conflicts with price override {who}")
    # 같은 (id, 날짜) 에 종류가 다른 교정이 겹치면 알림(오류는 아님: split drop + ledger set 은 양립 가능)
    for k, v in targets.items():
        if k[0] == "any" and len(v) > 1 and len({x.split(":")[0] for x in v}) > 1:
            iss.warn("(cross)", 0, ",".join(x.split(":")[1] for x in v), f"several override kinds touch id {k[1]} on {pd.Timestamp(k[2]).date()}: {v} (check they are consistent)")
    # X3 follow-up: ledger csv dividend cash (special_dividend 등, returns v2 가 지급) 와 lifecycle settle_cash/settle_mixed(events 가 지급) 가 같은 id 의
    # ±5 일 안에 있으면 이중계상 — 금액이 같거나 정산 현금 ≥ 배당 현금(배당을 포함) 이면 오류, 더 작으면(잔여 정산) 경고
    for k, v in targets.items():
        if k[0] != "ledger_cash":
            continue
        for (lex, lcash, loid, let, lfile, lrow) in v:
            for (sd, scash, soid, tr, sfile, srow) in targets.get(("lc_cash", k[1]), []):
                if pd.isna(sd) or pd.isna(lex) or abs((pd.Timestamp(sd) - pd.Timestamp(lex)).days) > 5:
                    continue
                msg = (f"ledger {let} cash {lcash:g} on {pd.Timestamp(lex).date()} ({loid}, {file_label(lfile)}:{lrow}) and lifecycle {tr} cash {scash:g} on "
                       f"{pd.Timestamp(sd).date()} ({soid}, {file_label(sfile)}:{srow}) for id {k[1]}: returns v2 pays the dividend and the engine pays the settlement")
                if not np.isfinite(lcash) or abs(scash - lcash) <= 1e-4 * max(1.0, abs(lcash)) or scash >= lcash:
                    iss.err(sfile, srow, soid, msg + " -> double count; keep only the residual (settlement cash < dividend cash) or drop one of them")
                else:
                    iss.warn(sfile, srow, soid, msg + " (settlement cash smaller: treated as the residual — confirm it excludes the dividend)")
    return summary, iss


def cmd_validate(args) -> int:
    ref = Ref(args.rc2)
    paths = []
    for p in args.csv:
        paths += sorted(glob.glob(p)) if any(ch in p for ch in "*?[") else [p]
    summary, iss = validate_files(paths, ref)
    print(pd.DataFrame(summary).to_string(index=False) if summary else "no files")
    if iss.rows:
        print()
        print(pd.DataFrame(iss.rows).to_string(index=False, max_colwidth=args.width))
    print(f"\nvalidate: {len(paths)} file(s), {sum(s.get('rows', 0) for s in summary)} row(s), {iss.n_err} error(s), {iss.n_warn} warning(s) -> "
          + ("FAIL" if iss.n_err else "OK"))
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(iss.rows, columns=["level", "file", "row", "overrideId", "message"]).to_csv(args.report, index=False)
    if args.conflicts:
        rows = []
        for r in iss.rows:
            m = r["message"]
            if r["level"] == "ERROR" and m.startswith("duplicate ") and " also in " in m:
                target = m.split(" also in ")[0].replace("duplicate ", "")
                other = m.split(" also in ")[1]
                rows.append({"kind": target.split(" ")[0], "target": target, "overrideId": r["overrideId"], "file": r["file"], "row": r["row"],
                             "conflictsWith": other, "crossTask": (other.split("/")[0] != r["file"].split("/")[0]) if "/" in other and "/" in r["file"] else ""})
            elif r["level"] == "WARN" and m.startswith("several override kinds touch"):
                rows.append({"kind": "cross-kind", "target": m.split(":")[0].replace("several override kinds touch ", ""), "overrideId": r["overrideId"], "file": "(cross)", "row": 0,
                             "conflictsWith": m.split(": ", 1)[1] if ": " in m else m, "crossTask": ""})
        Path(args.conflicts).parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows, columns=["kind", "target", "overrideId", "file", "row", "conflictsWith", "crossTask"]).to_csv(args.conflicts, index=False)
        print(f"conflicts: {sum(1 for x in rows if x['kind'] != 'cross-kind')} duplicate-target error(s), {sum(1 for x in rows if x['kind'] == 'cross-kind')} cross-kind notice(s) -> {args.conflicts}")
    return 1 if iss.n_err else 0


# --------------------------------------------------------------------------------------
# apply — 공통
# --------------------------------------------------------------------------------------
class ApplyLog:
    """rc2f_apply_log.csv: overrideId 마다 status applied / rejected / no_op, reason, affected_rows(바뀐 행·셀 수), target, kind, action."""

    COLS = ["overrideId", "caseId", "kind", "action", "status", "reason", "affected_rows", "target", "file", "eventIds"]

    def __init__(self):
        self.rows: dict[str, dict] = {}

    def set(self, oid: str, **kw):
        r = self.rows.setdefault(oid, {c: "" for c in self.COLS})
        r["overrideId"] = oid
        r.update(kw)

    def frame(self) -> pd.DataFrame:
        return pd.DataFrame(list(self.rows.values()), columns=self.COLS)


def _records(df: pd.DataFrame) -> list[dict]:
    return [{k: v for k, v in r.items()} for r in df.to_dict("records")]


def load_all_overrides(paths: list, ref: Ref) -> tuple[dict, list]:
    """validate → 종류별 DataFrame(문자열 그대로 + x_* 파싱 컬럼). 오류가 있으면 SystemExit."""
    summary, iss = validate_files(paths, ref)
    print(pd.DataFrame(summary).to_string(index=False) if summary else "no override files")
    if iss.rows:
        print(pd.DataFrame(iss.rows).to_string(index=False, max_colwidth=140))
    if iss.n_err:
        raise SystemExit(f"apply: {iss.n_err} validation error(s) — fix the override CSVs first (rc2f_apply.py validate)")
    by_kind = {k: [] for k in KINDS}
    for p in paths:
        kind = kind_of(p)
        df = read_override_csv(p)
        if not len(df):
            continue
        df = _common_checks(df, kind, ref, Issues())
        by_kind[kind].append(df)
    return {k: (pd.concat(v, ignore_index=True) if v else pd.DataFrame()) for k, v in by_kind.items()}, iss.rows


def _link(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink() or dst.exists():
        dst.unlink()
    os.symlink(Path(src).resolve(), dst)


def _tag_source(src, oid: str, estimate: bool = False, scaled: bool = False) -> str:
    """factors/split source 표기: |rc2f:<id>(확정) 또는 |rc2f_est:<id>(isEstimate=true → ledger_build 가 split 행을 estimated 로 표시) — 교정이 만든/옮긴/바꾼
    경계의 구간(sourceNext)에만. scaled=True 는 |rc2f_scaled:<id>: 교정으로 값만 재스케일된 이전 구간(경계 자체는 벤더 것 그대로 → 원장 status 규칙 비적용)."""
    if scaled:
        return f"{src}|rc2f_scaled:{oid}"
    return f"{src}|{'rc2f_est' if estimate else 'rc2f'}:{oid}"


# --------------------------------------------------------------------------------------
# price_overrides → base_data (T2 / aux)
# --------------------------------------------------------------------------------------
def _base_ids_of(path: Path) -> np.ndarray:
    pf = pq.ParquetFile(path)
    got = [np.unique(pf.read_row_group(rg, columns=["tradingItemId"]).column(0).to_numpy()) for rg in range(pf.num_row_groups)]
    return np.unique(np.concatenate(got)) if got else np.zeros(0, np.int64)


def apply_price_overrides(df: pd.DataFrame, src: Path, dst: Path, alog: ApplyLog, ids_in_file: set, label: str) -> dict:
    """base_data 행 (tradingItemId, dateFrom..dateTo) 의 fields 에 multiply/set_close 를 적용하고 qualityFlag = QF_PRICE_OVERRIDE(8).
    volume/source 는 그대로. 대상 id 가 이 파일에 없으면 이 파일에서는 건너뛴다(다른 파일이 담당). 바뀐 행이 0 이면 rejected(no_rows).
    row group 단위 스트리밍: 대상 id 가 없는 row group 은 그대로 다시 쓴다."""
    recs = []
    for r in df.itertuples(index=False):
        if r.x_tid not in ids_in_file:
            continue
        fields = tuple(x.strip() for x in (r.fields or "").replace(";", ",").split(",") if x.strip()) or PRICE_FIELDS
        recs.append(dict(oid=r.overrideId, tid=int(r.x_tid), d0=np.datetime64(parse_date(r.dateFrom), "ns"), d1=np.datetime64(parse_date(r.dateTo), "ns"),
                         op=r.op.strip(), value=float(r.value), fields=fields, caseId=r.caseId, estimate=bool(getattr(r, "x_isEstimate", False))))
    if not recs:
        return {"file": label, "overrides": 0, "rows_changed": 0, "rewritten": False}
    by_id: dict[int, list] = {}
    for rc in recs:
        by_id.setdefault(rc["tid"], []).append(rc)
        alog.set(rc["oid"], caseId=rc["caseId"], kind="price", action=rc["op"], status="rejected", reason="no_rows_in_range", affected_rows=0,
                 target=f"{rc['tid']} {str(rc['d0'])[:10]}..{str(rc['d1'])[:10]} {','.join(rc['fields'])}", file=label)
    pf = pq.ParquetFile(src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    writer = pq.ParquetWriter(tmp, pf.schema_arrow, compression="zstd", write_statistics=True, use_dictionary=False)
    counts = {rc["oid"]: 0 for rc in recs}
    n_total = 0
    id_idx = pf.schema_arrow.get_field_index("tradingItemId")
    for rg in range(pf.num_row_groups):
        st = pf.metadata.row_group(rg).column(id_idx).statistics
        t = pf.read_row_group(rg)
        ids = t.column("tradingItemId").to_numpy()
        lo, hi = (st.min, st.max) if st is not None and st.has_min_max else (int(ids.min()), int(ids.max()))
        hit = [tid for tid in by_id if lo <= tid <= hi]
        if hit:
            dates = t.column("date").to_numpy().astype("datetime64[ns]")
            cols = {f: t.column(f).to_numpy(zero_copy_only=False).copy() for f in PRICE_FIELDS}
            qf = t.column("qualityFlag").to_numpy().copy()
            changed_any = np.zeros(len(ids), dtype=bool)
            changed_est = np.zeros(len(ids), dtype=bool)
            for tid in hit:
                for rc in by_id[tid]:
                    m = (ids == tid) & (dates >= rc["d0"]) & (dates <= rc["d1"])
                    n = int(m.sum())
                    if n == 0:
                        continue
                    for fv, cnt in zip(*np.unique(qf[m], return_counts=True)):       # R-X0 P2-1: original qualityFlag values are kept in the log
                        rc.setdefault("orig_flags", {})[int(fv)] = rc.get("orig_flags", {}).get(int(fv), 0) + int(cnt)
                    for f in rc["fields"]:
                        v = cols[f]
                        if rc["op"] == "multiply":
                            v[m] = (v[m].astype(np.float64) * rc["value"]).astype(v.dtype)
                        else:
                            v[m] = np.array(rc["value"], dtype=v.dtype)
                    changed_any |= m
                    if rc["estimate"]:
                        changed_est |= m
                    counts[rc["oid"]] += n
            qf[changed_any] = QF_PRICE_OVERRIDE
            qf[changed_est] = QF_PRICE_OVERRIDE_EST          # estimated correction: a distinct bit so returns/ledger/recount carry the estimate downstream
            n_total += int(changed_any.sum())
            for f in PRICE_FIELDS:
                t = t.set_column(t.schema.get_field_index(f), f, pa.array(cols[f], type=t.schema.field(f).type))
            t = t.set_column(t.schema.get_field_index("qualityFlag"), "qualityFlag", pa.array(qf, type=pa.int8()))
        writer.write_table(t, row_group_size=max(1, t.num_rows))
    writer.close()
    os.replace(tmp, dst)
    for rc in recs:
        n = counts[rc["oid"]]
        qf_v = QF_PRICE_OVERRIDE_EST if rc["estimate"] else QF_PRICE_OVERRIDE
        alog.set(rc["oid"], status="applied" if n else "rejected", reason=(f"{rc['op']} {rc['value']:g} on {','.join(rc['fields'])}; qualityFlag={qf_v}{' (estimate: returns retFlag>=3, ledger split rows estimated)' if rc['estimate'] else ''} (was {rc.get('orig_flags', {})})" if n else "no_rows_in_range"),
                 affected_rows=n)
    return {"file": label, "overrides": len(recs), "rows_changed": n_total, "rewritten": True}


# --------------------------------------------------------------------------------------
# value_overrides → base_data (T2 / aux)
# --------------------------------------------------------------------------------------
def apply_value_overrides(df: pd.DataFrame, src: Path, dst: Path, alog: ApplyLog, ids_in_file: set, label: str, lc: pd.DataFrame, cal_days: np.ndarray) -> dict:
    """base_data (tradingItemId, date) 행의 field = value, 기존 행 qualityFlag 유지. 행이 없으면 거래일이고 lifecycle(lc, id 인덱스)
    [firstTradeDate, lastTradeDate(NaT = 열림)] 안일 때만 새 행(지정 안 된 필드 NaN, source = miprice 출처면 2 아니면 0)을 만들고
    master_aux.merge_base_data 로 (id, date) 정렬 유지. 아니면 rejected. src 를 읽어 dst 에 쓴다(dst 가 src 링크여도 됨)."""
    from master_aux import merge_base_data
    recs = []
    for r in df.itertuples(index=False):
        if r.x_tid not in ids_in_file:
            continue
        recs.append(dict(oid=r.overrideId, tid=int(r.x_tid), d=np.datetime64(parse_date(r.date), "ns"), field=str(r.field).strip(), value=float(r.value),
                         caseId=r.caseId, src_mi="miprice" in str(r.source)))
    if not recs:
        return {"file": label, "overrides": 0, "rows_set": 0, "rows_new": 0, "rewritten": False}
    by_id: dict[int, list] = {}
    for rc in recs:
        by_id.setdefault(rc["tid"], []).append(rc)
        alog.set(rc["oid"], caseId=rc["caseId"], kind="value", action=f"set_{rc['field']}", status="rejected", reason="no_row", affected_rows=0,
                 target=f"{rc['tid']} {str(rc['d'])[:10]} {rc['field']}", file=label)
    src = Path(src).resolve()
    pf = pq.ParquetFile(src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".val.tmp")
    writer = pq.ParquetWriter(tmp, pf.schema_arrow, compression="zstd", write_statistics=True, use_dictionary=False)
    done: set = set()
    rows_set = 0
    id_idx = pf.schema_arrow.get_field_index("tradingItemId")
    for rg in range(pf.num_row_groups):
        st = pf.metadata.row_group(rg).column(id_idx).statistics
        t = pf.read_row_group(rg)
        ids = t.column("tradingItemId").to_numpy()
        lo, hi = (st.min, st.max) if st is not None and st.has_min_max else (int(ids.min()), int(ids.max()))
        hit = [tid for tid in by_id if lo <= tid <= hi]
        if hit:
            dates = t.column("date").to_numpy().astype("datetime64[ns]")
            cols = {f: t.column(f).to_numpy(zero_copy_only=False).copy() for f in VALUE_FIELDS}
            setm = {f: np.zeros(len(ids), dtype=bool) for f in VALUE_FIELDS}      # 실제로 값이 설정된 셀(이 외 셀의 null 은 보존)
            qf = t.column("qualityFlag").to_numpy().copy()
            changed = np.zeros(len(ids), dtype=bool)
            for tid in hit:
                for rc in by_id[tid]:
                    m = (ids == tid) & (dates == rc["d"])
                    if not m.any():
                        continue
                    v = cols[rc["field"]]
                    v[m] = np.array(rc["value"], dtype=v.dtype)
                    setm[rc["field"]] |= m
                    changed |= m
                    done.add(rc["oid"])
                    alog.set(rc["oid"], status="applied", reason=f"set {rc['field']}={rc['value']:g}; qualityFlag kept {int(qf[m][0])}", affected_rows=int(m.sum()))
            rows_set += int(changed.sum())
            for f in VALUE_FIELDS:
                if not setm[f].any():
                    continue
                nulls = t.column(f).is_null().to_numpy(zero_copy_only=False) & ~setm[f]
                t = t.set_column(t.schema.get_field_index(f), f, pa.array(cols[f], type=t.schema.field(f).type, mask=nulls))
        writer.write_table(t, row_group_size=max(1, t.num_rows))
    writer.close()
    # 새 행: 기존 행이 없는 (id, date) — 거래일 ∧ lifecycle 안일 때만
    days = np.asarray(cal_days).astype("datetime64[D]")
    new: dict = {}
    for rc in recs:
        if rc["oid"] in done:
            continue
        d = rc["d"].astype("datetime64[D]")
        i = int(np.searchsorted(days, d))
        if i >= len(days) or days[i] != d:
            alog.set(rc["oid"], reason="no_row; not a trading day (no new row)")
            continue
        ft = pd.to_datetime(lc.at[rc["tid"], "firstTradeDate"]) if rc["tid"] in lc.index else pd.NaT
        lt = pd.to_datetime(lc.at[rc["tid"], "lastTradeDate"]) if rc["tid"] in lc.index else pd.NaT
        if pd.isna(ft) or pd.Timestamp(rc["d"]) < ft or (pd.notna(lt) and pd.Timestamp(rc["d"]) > lt):
            alog.set(rc["oid"], reason=f"no_row; outside lifecycle [{ft}, {lt}] (no new row)")
            continue
        row = new.setdefault((rc["tid"], rc["d"]), {"date": rc["d"], "tradingItemId": rc["tid"], **{f: np.nan for f in VALUE_FIELDS},
                                                    "source": SOURCE_MI if rc["src_mi"] else 0, "qualityFlag": QF_VALUE_OVERRIDE})
        row[rc["field"]] = rc["value"]
        alog.set(rc["oid"], status="applied", reason=f"new row; set {rc['field']}={rc['value']:g}; qualityFlag={QF_VALUE_OVERRIDE}", affected_rows=1)
    if new:
        add = pd.DataFrame(list(new.values()))
        add_path = dst.with_name(dst.name + ".val_new.parquet")
        pq.write_table(pa.Table.from_pandas(add[pf.schema_arrow.names], schema=pf.schema_arrow, preserve_index=False), add_path)
        tmp2 = dst.with_name(dst.name + ".val2.tmp")
        merge_base_data(tmp, add_path, tmp2)
        os.replace(tmp2, tmp)
        add_path.unlink()
    if dst.is_symlink() or dst.exists():
        dst.unlink()
    os.replace(tmp, dst)
    return {"file": label, "overrides": len(recs), "rows_set": rows_set, "rows_new": len(new), "rewritten": True}


# --------------------------------------------------------------------------------------
# split_overrides → factors/split (T2 / aux)
# --------------------------------------------------------------------------------------
def _split_one(g: pd.DataFrame, rec: dict, cal) -> tuple[pd.DataFrame, int, str]:
    """한 id 의 구간(validFrom 정렬) 에 교정 1건. 반환 (새 구간, 바뀐 행 수, reason). 계수는 스냅샷(현재 주식수) 기준 누적이므로
    경계 이전 구간을 스케일해 현재 기준을 보존한다(이전 경계들의 비율은 유지)."""
    g = g.sort_values("validFrom").reset_index(drop=True)
    ex, act, oid = rec["exDate"], rec["action"], rec["oid"]
    vf = pd.to_datetime(g.validFrom)
    vt = pd.to_datetime(g.validTo)
    fac = g.factor.to_numpy(dtype=float).copy()
    if act == "add":
        nf = rec["newFactor"]
        if (vf == ex).any():
            return g, 0, "reject:boundary_exists"
        inside = (vf <= ex) & (vt.isna() | (vt > ex))
        if not inside.any():
            return g, 0, "reject:no_interval_contains_exDate"
        i = int(np.flatnonzero(inside.to_numpy())[0])
        fac[:i] *= nf
        first = g.iloc[i].copy(); second = g.iloc[i].copy()
        first["validTo"] = ex; first["factor"] = fac[i] * nf; first["source"] = _tag_source(first["source"], oid, scaled=True)      # 이전 구간: 값만 재스케일
        second["validFrom"] = ex; second["factor"] = fac[i]; second["source"] = _tag_source(second["source"], oid, rec.get("estimate", False))   # sourceNext = ledger split row src
        rows = [g.iloc[k].copy() for k in range(i)]
        for k, r in enumerate(rows):
            r["factor"] = fac[k]
        rows += [first, second] + [g.iloc[k].copy() for k in range(i + 1, len(g))]
        out = pd.DataFrame(rows).reset_index(drop=True)
        return out, i + 2, f"add boundary {ex.date()} newFactor {nf:g}: {i + 1} earlier interval(s) x{nf:g}"
    j_arr = np.flatnonzero((vf == ex).to_numpy())
    if not len(j_arr):
        al, _ = cal.align(vf.to_numpy())
        j_arr = np.flatnonzero(pd.to_datetime(al) == ex)
    if not len(j_arr) or j_arr[0] == 0:
        return g, 0, "reject:boundary_not_found"
    j = int(j_arr[0])
    fp, fn = fac[j - 1], fac[j]
    if fp == fn:
        return g, 0, "reject:not_a_boundary(equal_factors)"
    if act == "confirm":
        return g, 0, f"confirm(no change; parentFactor {fp / fn:g})"
    if act == "drop":
        fac[:j] *= fn / fp
        fac[j - 1] = fn
        g2 = g.copy(); g2["factor"] = fac
        merged = g2.iloc[j - 1].copy()
        merged["validTo"] = g2.iloc[j].validTo
        merged["source"] = _tag_source(f"{g2.iloc[j - 1].source}+{g2.iloc[j].source}", oid, scaled=True)      # 병합 구간: 남는 경계는 벤더 것
        out = pd.concat([g2.iloc[:j - 1], pd.DataFrame([merged]), g2.iloc[j + 1:]], ignore_index=True)
        return out, j, f"drop boundary {ex.date()} (factor {fp:g}->{fn:g} removed): {j - 1} earlier interval(s) x{fn / fp:g}, intervals merged"
    if act == "set_factor":
        nf = rec["newFactor"]
        old_pf = fp / fn
        if abs(old_pf - nf) <= 1e-12:
            return g, 0, f"no_op(parentFactor already {nf:g})"
        fac[:j] *= nf * fn / fp
        g2 = g.copy(); g2["factor"] = fac
        g2.loc[j, "source"] = _tag_source(g2.loc[j, "source"], oid, rec.get("estimate", False))
        return g2, j, f"set_factor {ex.date()}: parentFactor {old_pf:g} -> {nf:g}; {j} earlier interval(s) x{nf * fn / fp:g}"
    if act == "move_date":
        nx = rec["newExDate"]
        prev_from = vf.iloc[j - 1]
        next_from = vf.iloc[j + 1] if j + 1 < len(g) else pd.NaT
        if nx <= prev_from or (pd.notna(next_from) and nx >= next_from):
            return g, 0, "reject:newExDate_crosses_neighbour"
        g2 = g.copy()
        g2.loc[j - 1, "validTo"] = nx
        g2.loc[j, "validFrom"] = nx
        g2.loc[j, "source"] = _tag_source(g2.loc[j, "source"], oid, rec.get("estimate", False))
        return g2, 2, f"move_date {ex.date()} -> {nx.date()}"
    return g, 0, f"reject:unknown_action {act}"


SPLIT_JUMP_TOL = 0.25            # = ledger_build.JUMP_TOL: raw close_ex / close_prev x parentFactor 가 1 에서 벗어나도 되는 폭


SPLIT_CHECK_TDAYS = 5            # Y2a: raw 점프 검사 창 — 경계 ±5 거래일 안에 직전/이후 raw 종가가 있어야 검사 가능(없으면 uncheckable)
CHECK_BASIS = ("raw_jump", "mi_ratio", "document", "none")     # split CSV 선택 컬럼 checkBasis: 교정 근거의 종류(Y2a 검증기 규칙)


def _raw_jump(closes, tid: int, day, cal=None) -> tuple[float, str]:
    """(raw close on the boundary day or the first within SPLIT_CHECK_TDAYS trading days after) / (last raw close within SPLIT_CHECK_TDAYS trading
    days before). Returns (jump, why): jump NaN with why = 'no_close_before(±5td)' / 'no_close_on_or_after(±5td)' when uncheckable."""
    if closes is None or day is None or pd.isna(day):
        return np.nan, "no_closes"
    day = pd.Timestamp(day)
    d = np.array([np.datetime64(day, "ns")])
    if cal is not None and hasattr(cal, "days"):
        days = np.asarray(cal.days, dtype="datetime64[D]")
        k = int(np.searchsorted(days, np.datetime64(day, "D"), side="left"))
        lo = pd.Timestamp(days[max(k - SPLIT_CHECK_TDAYS, 0)])
        hi = pd.Timestamp(days[min(k + SPLIT_CHECK_TDAYS, len(days) - 1)])
        cal_span = int((hi - day).days)
    else:
        lo, hi, cal_span = day - pd.Timedelta(days=7), day + pd.Timedelta(days=7), 7
    c_ex = closes.at(np.array([tid]), d)
    d_ex = day
    if not np.isfinite(c_ex[0]):
        c_ex, dd = closes.on_or_after(np.array([tid]), d, max(cal_span, 1))
        d_ex = pd.Timestamp(dd[0]) if np.isfinite(c_ex[0]) else pd.NaT
    c_prev, d_prev = closes.before(np.array([tid]), d)
    if not np.isfinite(c_prev[0]) or pd.isna(d_prev[0]) or pd.Timestamp(d_prev[0]) < lo or c_prev[0] <= 0:
        return np.nan, f"no_close_before(within {SPLIT_CHECK_TDAYS} trading days of {day.date()})"
    if not np.isfinite(c_ex[0]) or pd.isna(d_ex) or d_ex > hi:
        return np.nan, f"no_close_on_or_after(within {SPLIT_CHECK_TDAYS} trading days of {day.date()})"
    return float(c_ex[0] / c_prev[0]), f"{pd.Timestamp(d_prev[0]).date()}:{float(c_prev[0]):.4g}->{d_ex.date()}:{float(c_ex[0]):.4g}"


def split_override_check(rec: dict, g_before: pd.DataFrame, g_after: pd.DataFrame, closes, cal=None) -> tuple[str, str]:
    """Y2a 3상태 raw 점프 검사(R-X0 2차 P1-7 의 확장): 경계가 없어진 날(drop, move_date 의 옛 날짜)은 raw 가 연속(|jump − 1| ≤ SPLIT_JUMP_TOL)이어야
    하고, 경계가 있는 날(set_factor, add, move_date 의 새 날짜)은 jump × parentFactor ≈ 1 이어야 한다.
    반환 (state, detail): state ∈ {'pass', 'fail', 'uncheckable'} — uncheckable = 검사할 경계 어느 쪽이든 ±SPLIT_CHECK_TDAYS 거래일 안에 raw 종가가 없음
    (closes 자체가 없어도 uncheckable). 여러 경계(move_date) 는 하나라도 fail 이면 fail, 아니면 하나라도 uncheckable 이면 uncheckable.
    로그: pass → applied, fail → applied_unverified, uncheckable → applied_unchecked (적용 자체는 셋 다 됨)."""
    tid, act = rec["tid"], rec["action"]
    if closes is None:
        return "uncheckable", "no_price_data(closes not provided)"
    notes = []
    states = []

    def pf_at(g, day):
        # R-Y2 P2: the boundary's validFrom may sit on a non-trading day (calendar-aligned exDate) -> nearest validFrom within 5 calendar days
        gg = g.sort_values("validFrom").reset_index(drop=True)
        vf = pd.to_datetime(gg.validFrom)
        dist = (vf - pd.Timestamp(day)).abs().dt.days
        if not len(gg) or dist.min() > 5:
            return np.nan
        i = int(dist.idxmin())
        return float(gg.factor.iloc[i - 1]) / float(gg.factor.iloc[i]) if i > 0 else np.nan

    def prints_between(a, b):
        """raw closes strictly inside (min(a,b), max(a,b)) — a move_date inside a print gap has no independent old-boundary check."""
        lo, hi = min(pd.Timestamp(a), pd.Timestamp(b)), max(pd.Timestamp(a), pd.Timestamp(b))
        c, d = closes.on_or_after(np.array([tid]), np.array([np.datetime64(lo + pd.Timedelta(days=1), "ns")]), max(int((hi - lo).days), 1))
        return bool(np.isfinite(c[0]) and not pd.isna(d[0]) and pd.Timestamp(d[0]) < hi)
    if act == "move_date" and rec.get("newExDate") is not None and not prints_between(rec["exDate"], rec["newExDate"]):
        # R-Y2 P2: the old and the new boundary sit inside the same print gap: the "removed boundary" check would compare the same two prints
        # as the new-boundary check (structurally fail) -> the old-boundary check is not applicable; only the new boundary is checked
        notes.append(f"removed_boundary {pd.Timestamp(rec['exDate']).date()}: skipped(no raw print between {pd.Timestamp(rec['exDate']).date()} and {pd.Timestamp(rec['newExDate']).date()}: same interval as the new boundary)")
    elif act in ("drop", "move_date"):
        j, why = _raw_jump(closes, tid, rec["exDate"], cal)
        pf_old = pf_at(g_before, rec["exDate"])
        if not np.isfinite(j):
            states.append("uncheckable"); notes.append(f"removed_boundary {pd.Timestamp(rec['exDate']).date()}: uncheckable({why})")
        elif abs(j - 1.0) > SPLIT_JUMP_TOL:
            states.append("fail")
            notes.append(f"removed_boundary {pd.Timestamp(rec['exDate']).date()}: raw_jump={j:.4g} ({why}; expected ~1)" + (f"; x old parentFactor {pf_old:.4g} = {j * pf_old:.4g}: the vendor step matched the price move" if np.isfinite(pf_old) else ""))
        else:
            states.append("pass"); notes.append(f"removed_boundary {pd.Timestamp(rec['exDate']).date()}: raw_jump={j:.4g} ({why}; continuous)")
    if act in ("set_factor", "add", "move_date"):
        day = rec["newExDate"] if act == "move_date" else rec["exDate"]
        j, why = _raw_jump(closes, tid, day, cal)
        pf = pf_at(g_after, day)
        if not np.isfinite(j) or not np.isfinite(pf):
            states.append("uncheckable"); notes.append(f"boundary {pd.Timestamp(day).date()}: uncheckable({why if not np.isfinite(j) else 'no parentFactor'})")
        elif abs(j * pf - 1.0) > SPLIT_JUMP_TOL:
            states.append("fail"); notes.append(f"boundary {pd.Timestamp(day).date()}: raw_jump_x_parentFactor={j * pf:.4g} (jump {j:.4g} [{why}] x pf {pf:.4g}; expected ~1)")
        else:
            states.append("pass"); notes.append(f"boundary {pd.Timestamp(day).date()}: raw_jump_x_parentFactor={j * pf:.4g} (jump {j:.4g} [{why}] x pf {pf:.4g})")
    state = "fail" if "fail" in states else ("uncheckable" if "uncheckable" in states else "pass")
    return state, "; ".join(notes)


SPLIT_CHECK_STATUS = {"pass": "applied", "fail": "applied_unverified", "uncheckable": "applied_unchecked"}


def apply_split_overrides(df: pd.DataFrame, tables: dict, alog: ApplyLog, cal, closes=None, price_ids_by_tid: dict | None = None) -> dict:
    """tables: {label: (DataFrame, ids set)}. 각 교정을 id 가 속한 표에 적용. 반환 {label: (new_df, changed bool)}.
    closes(RawCloses, 선택): 교정 뒤 경계와 raw 점프의 정합 검사(split_override_check, 3상태) → status applied / applied_unverified / applied_unchecked.
    price_ids_by_tid {tid: [price overrideIds]}: 같은 실행의 price 교정 뒤 가격으로 재검사했음을 reason 에 남긴다(검증기의 deferred 행 확정, R-Y2 P2)."""
    from prices_factors import check_intervals
    out = {k: [v[0].copy(), False] for k, v in tables.items()}
    for r in df.itertuples(index=False):
        rec = dict(oid=r.overrideId, tid=int(r.x_tid), exDate=parse_date(r.exDate), action=r.action.strip(),
                   newFactor=parse_float(r.newFactor), newExDate=parse_date(r.newExDate), caseId=r.caseId, estimate=bool(r.x_isEstimate))
        label = next((k for k, v in tables.items() if rec["tid"] in v[1]), None)
        alog.set(rec["oid"], caseId=rec["caseId"], kind="split", action=rec["action"], target=f"{rec['tid']}@{rec['exDate'].date()}", file=label or "")
        if label is None:
            alog.set(rec["oid"], status="rejected", reason="id_not_in_factors", affected_rows=0)
            continue
        t = out[label][0]
        g = t[t.tradingItemId == rec["tid"]]
        g2, n, reason = _split_one(g, rec, cal)
        if reason.startswith("reject"):
            alog.set(rec["oid"], status="rejected", reason=reason, affected_rows=0)
            continue
        if n == 0:
            alog.set(rec["oid"], status="no_op", reason=reason, affected_rows=0)
            continue
        out[label][0] = pd.concat([t[t.tradingItemId != rec["tid"]], g2[t.columns]], ignore_index=True).sort_values(["tradingItemId", "validFrom"]).reset_index(drop=True)
        out[label][1] = True
        state, why = split_override_check(rec, g, g2, closes, cal)
        px_note = f" [re-checked after price override(s) {','.join(price_ids_by_tid[rec['tid']])}]" if price_ids_by_tid and rec["tid"] in price_ids_by_tid else ""
        alog.set(rec["oid"], status=SPLIT_CHECK_STATUS[state], reason=reason + f"; check={state}: {why}{px_note}", affected_rows=n)
    for label, (t, changed) in out.items():
        if changed:
            for c in ("validFrom", "validTo", "snapshotDate"):
                t[c] = pd.to_datetime(t[c]).astype("datetime64[ns]")
            t["factor"] = t.factor.astype("float64")
            t["tradingItemId"] = t.tradingItemId.astype("int64")
            chk = check_intervals(t)
            if not chk["ok"]:
                raise SystemExit(f"split overrides broke the interval contract of {label}: {chk['violations']} {chk['examples'][:3]}")
            out[label][0] = t
    return {k: (v[0], v[1]) for k, v in out.items()}


# --------------------------------------------------------------------------------------
# lineage_overrides → master/lineage.parquet
# --------------------------------------------------------------------------------------
LINK_CLASS = {"rename": "simple_rename", "relist": "relist", "merger": "merger"}
MAX_CHAIN_GAP = 5


def _chain_gap(cal_days: np.ndarray, lc: pd.DataFrame, old: int, new: int):
    import master_lifecycle as ml
    lo = lc.lastTradeDate.get(old, pd.NaT) if old in lc.index else pd.NaT
    fn = lc.firstTradeDate.get(new, pd.NaT) if new in lc.index else pd.NaT
    if pd.isna(lo) or pd.isna(fn):
        return pd.NA
    if fn <= lo:
        return 0
    return int(ml.tdays_between(cal_days, np.array([lo], dtype="datetime64[ns]"), np.array([fn], dtype="datetime64[ns]"))[0]) - 1


def apply_lineage_overrides(df: pd.DataFrame, ln: pd.DataFrame, lc: pd.DataFrame, cal_days: np.ndarray, alog: ApplyLog) -> pd.DataFrame:
    """set_ratio: ratio·ratioSource('rc2f_override' / 'rc2f_estimate' when isEstimate)·confidence·evidence·version·chainable 재계산.
    set_chainable: chainable. drop: 행 제거. add: 행 추가(chainGapTradingDays 는 lifecycle 로 계산, chainable = 조건식 또는 chainable 컬럼).
    lineage 는 ledger_build(rename/relist 행)·prices_returns(첫 행 링크)·master_aux 가 읽는다."""
    ln = ln.copy()
    if "linkVerified" not in ln.columns:
        ln["linkVerified"] = False          # rc2f-E: X5-verified genuine first-day link move (prices_returns keeps the regular flag instead of 4)
    for r in df.itertuples(index=False):
        old, new = parse_int(r.oldId), parse_int(r.newId)
        act = r.action.strip()
        oid = r.overrideId
        alog.set(oid, caseId=r.caseId, kind="lineage", action=act, target=f"{old}->{new}", file="lineage")
        m = (ln.oldId == old) & (ln.newId == new)
        ev = f"rc2f:{oid}: {r.evidence}"
        lv_txt = str(getattr(r, "linkVerified", "") or "").strip()
        if lv_txt.lower() in ("nan", "none", "null"):     # column absent in some task files -> NaN after concat
            lv_txt = ""
        lv = parse_bool(lv_txt) if lv_txt else None
        if act == "add":
            rel = r.relation.strip()
            ratio = parse_float(r.ratio)
            gap = _chain_gap(cal_days, lc, old, new)
            chainable = True if rel in ("rename", "relist") else False          # lead decision (X5 handover): added same-line links are chainable
            if r.chainable.strip():
                chainable = parse_bool(r.chainable)
            row = {"oldId": old, "newId": new, "relation": rel, "effectiveDate": parse_date(r.effectiveDate), "ratio": ratio, "evidence": ev[:600],
                   "confidence": "medium" if r.x_isEstimate else "high", "ratioSource": (f"rc2f_estimate:{oid}" if r.x_isEstimate else f"rc2f_override:{oid}"),
                   "linkClass": LINK_CLASS.get(rel, rel), "reviewFlag": "", "sources": "rc2f", "version": "rc2f", "chainGapTradingDays": gap, "chainable": bool(chainable),
                   "linkVerified": bool(lv) if lv is not None else False}
            ln = pd.concat([ln, pd.DataFrame([row])[ln.columns]], ignore_index=True)
            alog.set(oid, status="applied", reason=f"added {rel} ratio {ratio} eff {row['effectiveDate'].date()} chainGap {gap} chainable {chainable}" + (f" linkVerified {lv}" if lv is not None else ""), affected_rows=1)
            continue
        idx = ln.index[m]
        if not len(idx):
            alog.set(oid, status="rejected", reason="pair_not_found", affected_rows=0)
            continue
        i = idx[0]
        if act == "drop":
            ln = ln.drop(index=idx)
            alog.set(oid, status="applied", reason=f"dropped {len(idx)} row(s)", affected_rows=int(len(idx)))
            continue
        if act == "set_ratio":
            # lead decision (X5 handover): set_ratio confirms the link -> chainable True (the generator still requires rename/relist and chainGap <= 5);
            # isEstimate false = ratio confirmed (ratioSource rc2f_override:<id>), true = ratio updated + estimate flag 3 (ratioSource rc2f_estimate:<id>)
            ratio = parse_float(r.ratio)
            before = ln.at[i, "ratio"]
            ln.at[i, "ratio"] = ratio
            ln.at[i, "ratioSource"] = f"rc2f_estimate:{oid}" if r.x_isEstimate else f"rc2f_override:{oid}"
            ln.at[i, "confidence"] = "medium" if r.x_isEstimate else "high"
            if r.effectiveDate.strip():
                ln.at[i, "effectiveDate"] = parse_date(r.effectiveDate)
            ln.at[i, "chainable"] = bool(ln.at[i, "relation"] in ("rename", "relist"))
            gap = ln.at[i, "chainGapTradingDays"]
            if ln.at[i, "chainable"] and pd.notna(gap) and int(gap) > MAX_CHAIN_GAP:
                # R-X0 P1-4: the generator links only chainGapTradingDays <= 5 -> the ratio is recorded but no first-row link results
                alog.set(oid, status="applied_no_effect", reason=f"ratio {before} -> {ratio} ({ln.at[i, 'ratioSource']}); not_linked(chain_gap {int(gap)} > {MAX_CHAIN_GAP}): ledger rename/relist row updated, returns first row stays NaN", affected_rows=1)
            else:
                alog.set(oid, status="applied", reason=f"ratio {before} -> {ratio} ({ln.at[i, 'ratioSource']}), chainable {ln.at[i, 'chainable']}", affected_rows=1)
        elif act == "set_chainable":
            v = parse_bool(r.chainable)
            before = bool(ln.at[i, "chainable"])
            ln.at[i, "chainable"] = v
            alog.set(oid, status="applied" if before != v else "no_op", reason=f"chainable {before} -> {v}", affected_rows=int(before != v))
        if lv is not None and act != "drop":
            ln.at[i, "linkVerified"] = bool(lv)
            cur = alog.rows[oid].get("reason", "")
            alog.set(oid, reason=(cur + "; " if cur else "") + f"linkVerified {lv}")
        ln.at[i, "evidence"] = (str(ln.at[i, "evidence"]) + " | " + ev)[:600]
        ln.at[i, "version"] = "rc2f"
        ln.at[i, "sources"] = (str(ln.at[i, "sources"]) + "|rc2f") if ln.at[i, "sources"] else "rc2f"
    ln["chainGapTradingDays"] = ln.chainGapTradingDays.astype("Int64")
    ln["chainable"] = ln.chainable.astype(bool)
    ln["linkVerified"] = ln.linkVerified.fillna(False).astype(bool)
    ln["effectiveDate"] = pd.to_datetime(ln.effectiveDate).astype("datetime64[ns]")
    return ln.sort_values(["oldId", "newId"]).reset_index(drop=True)


# --------------------------------------------------------------------------------------
# lifecycle_overrides → lifecycle / lifecycle_dates / events(corporate_actions) / lineage
# --------------------------------------------------------------------------------------
LC_END_REASON = {"cash_merger": "merger", "stock_merger": "merger", "mixed_merger": "merger", "id_change": "merger", "halt_suspension": "delist_other",
                 "bankruptcy": "bankruptcy", "liquidation": "delist_other", "data_gap": "data_gap", "delist_other": "delist_other", "still_trading": "active"}
ACTION_CODE = {"cash_merger": "ACQ", "stock_merger": "ACQ", "mixed_merger": "ACQ", "id_change": "N/C", "halt_suspension": "OTH", "bankruptcy": "BK11",
               "liquidation": "LIQ", "data_gap": "OTH", "delist_other": "OTH", "still_trading": "OTH"}


def _events_row_defaults(cols) -> dict:
    row = {c: None for c in cols}
    row.update({"settlementReturn": np.nan, "suggestedReturn": np.nan, "cashPerShare": np.nan, "successorTradingItemId": pd.NA, "successorRatio": np.nan,
                "acquirerName": None, "source": "rc2f_override", "confidence": "override", "actionText": None, "matchMethod": "override",
                "endDateSource": "override", "endDateConflict": False, "dbEndDateReason": "", "endDateGapDays": 0, "reviewFlag": "", "endDateBasis": "override",
                "acquirerPriceDate": pd.NaT, "hasCashLeg": False, "hasStockLeg": False, "cacheUnitStatus": "no_terms", "overrideSource": "",
                "cashPerShareCache": np.nan, "successorRatioCache": np.nan, "cacheUnitFactorTarget": np.nan, "cacheUnitFactorSuccessor": np.nan,
                "targetCacheMark": np.nan, "targetCacheMarkDate": pd.NaT, "cacheUnitFactorTargetDate": pd.NaT, "successorCacheClose": np.nan,
                "successorCacheCloseDate": pd.NaT, "settlementDate": pd.NaT, "endDate": pd.NaT, "endDateOriginal": pd.NaT, "actionCode": None})
    if "settlementReturnScope" in cols:
        row["settlementReturnScope"] = "full"
    return row


def _patch_events(ev: pd.DataFrame, tid: int, rec: dict, closes, cal, oid: str) -> tuple[pd.DataFrame, str]:
    """events(corporate_actions) 한 id 행 교정. 바뀌는 컬럼: endDate(=lastTradeDate; 엔진 정산은 그 다음 bar), endDateOriginal(=effectiveDate),
    endDateSource='override', overrideSource, settlementDate(settlementDate>payDate 순), 조건(cashPerShare/successorTradingItemId/successorRatio/
    hasCashLeg/hasStockLeg), 캐시 단위 컬럼(raw 캐시 k=1: *Cache = raw 값, targetCacheMark = 대상의 endDate 이전 마지막 raw 종가,
    successorCacheClose = 정산 bar 이후 7일 내 승계사 첫 raw 종가), settlementReturn = (cash + ratio×succ)/mark − 1,
    cacheUnitStatus, source/confidence/actionText/reviewFlag.
    treatment → events 매핑(리드 결정, X4 인계 §5; 엔진 simulator.cpp:289~298 는 settlementReturn NaN → default_delisting_return 0 = 마지막 종가 청산):
      settle_cash        cashPerShare/cashPerShareCache = cash, hasCashLeg, successor 없음, settlementReturn = cash/mark − 1
      convert_successor  successorTradingItemId/successorRatio(+Cache), hasStockLeg, settlementReturn = ratio×succ/mark − 1(승계 종가 없으면 NaN → 엔진 0)
      settle_mixed       현금 + 승계 leg 둘 다(partial 두 leg 를 한 행이 보유), settlementReturn = (cash + ratio×succ)/mark − 1
      last_close         leg 없음, settlementReturn NaN 유지(엔진 0 = 마지막 종가 청산), cacheUnitStatus last_close(근거 있는 확정 — thin 은 전환기 항목으로 세지 않음)
      unresolved         행 유지/생성, leg 없음, settlementReturn NaN + reviewFlag rc2f_unresolved(보유 시 검토 대상; lifecycle_dates.settlementDate 는 비움)
      extend_prices      endDate 를 새 lastTradeDate 로 이동(가격은 apply_extend_prices 가 보충), 조건 불변
    반환 (events, 설명)."""
    tr = rec["treatment"]
    has = tid in ev.tradingItemId.values
    # R-X4 §4-3 (lead decision): extend_prices = stage 1 price supplement (apply_extend_prices) + stage 2 = the terms carried by the row applied at the
    # new lastTradeDate: cash+successor -> settle_mixed, cash -> settle_cash, successor -> convert_successor; no terms -> the existing events terms are kept
    # but re-priced at the new endDate (mark / settlement bar / successor close), no events row -> stays absent (last close, engine default 0)
    eff = tr
    if tr == "extend_prices":
        has_cash = rec["cashPerShare"] is not None and np.isfinite(rec["cashPerShare"])
        has_succ = rec["successor"] is not None
        eff = "settle_mixed" if (has_cash and has_succ) else ("settle_cash" if has_cash else ("convert_successor" if has_succ else "extend_prices"))
    if not has:
        if eff == "extend_prices":
            return ev, "no events row (extend_prices without terms keeps it absent)"
        row = _events_row_defaults(ev.columns)
        row["tradingItemId"] = tid
        ev = pd.concat([ev, pd.DataFrame([row])[ev.columns]], ignore_index=True)
        created = True
    else:
        created = False
    i = ev.index[ev.tradingItemId == tid][0]
    cur_end = pd.to_datetime(ev.at[i, "endDate"]) if pd.notna(ev.at[i, "endDate"]) else pd.NaT
    end = rec["lastTradeDate"] if rec["lastTradeDate"] is not None else (cur_end if pd.notna(cur_end) else rec["lifecycleLast"])
    if pd.isna(end):
        return ev, "reject:no_end_date"
    ev.at[i, "endDate"] = pd.Timestamp(end)
    if rec["effectiveDate"] is not None:
        ev.at[i, "endDateOriginal"] = rec["effectiveDate"]
    elif pd.isna(ev.at[i, "endDateOriginal"]):
        ev.at[i, "endDateOriginal"] = pd.Timestamp(end)
    ev.at[i, "endDateSource"] = "override"
    ev.at[i, "overrideSource"] = f"rc2f:{oid}"
    ev.at[i, "endDateGapDays"] = int((pd.Timestamp(ev.at[i, "endDateOriginal"]) - pd.Timestamp(end)).days) if pd.notna(ev.at[i, "endDateOriginal"]) else 0
    sd = rec["settlementDate"] if rec["settlementDate"] is not None else (rec["payDate"] if rec["payDate"] is not None else None)
    if sd is not None:
        ev.at[i, "settlementDate"] = sd
    elif created or pd.isna(ev.at[i, "settlementDate"]) or pd.Timestamp(ev.at[i, "settlementDate"]) <= pd.Timestamp(end):
        # R-X0 P2-3: an existing settlementDate on/before the (moved) endDate cannot be the settlement bar -> next trading day after endDate
        nxt, _ = cal.align(np.array([np.datetime64(pd.Timestamp(end) + pd.Timedelta(days=1), "ns")]))
        ev.at[i, "settlementDate"] = pd.Timestamp(nxt[0])
    # actionCode: 조건이 있는 treatment 는 endReason 매핑(ACQ/LIQ/BK11/N/C…; ledger_build 는 N/C 를 lineage 담당으로 보고 merger 행을 만들지 않음)
    if eff in ("settle_cash", "convert_successor", "settle_mixed") or created or not (isinstance(ev.at[i, "actionCode"], str) and ev.at[i, "actionCode"]):
        ev.at[i, "actionCode"] = ACTION_CODE[rec["endReason"]]
    # 마크·정산 bar·승계사 종가(raw, k=1)
    mark, mark_d = closes.before(np.array([tid]), np.array([np.datetime64(pd.Timestamp(end) + pd.Timedelta(days=1), "ns")]))
    mark, mark_d = float(mark[0]), pd.Timestamp(mark_d[0])
    settle_bar, _ = cal.align(np.array([np.datetime64(pd.Timestamp(end) + pd.Timedelta(days=1), "ns")]))
    settle_bar = pd.Timestamp(settle_bar[0])
    ev.at[i, "targetCacheMark"] = mark
    ev.at[i, "targetCacheMarkDate"] = mark_d
    ev.at[i, "cacheUnitFactorTarget"] = 1.0 if np.isfinite(mark) else np.nan
    ev.at[i, "cacheUnitFactorTargetDate"] = mark_d
    cash = rec["cashPerShare"] if eff in ("settle_cash", "settle_mixed") else np.nan
    succ = rec["successor"] if eff in ("convert_successor", "settle_mixed") else None
    ratio = rec["successorRatio"] if succ is not None else np.nan
    if succ is not None and (ratio is None or not np.isfinite(float(ratio)) or float(ratio) <= 0):
        raise ValueError(f"{oid}: lifecycle override for id {tid} names successor {succ} without a positive successorRatio (treatment {tr}); "
                         f"validate rejects this row — the events patch needs ratio x successor close")
    if eff == "extend_prices":
        # existing terms kept, re-priced at the moved endDate
        cash = float(ev.at[i, "cashPerShare"]) if bool(ev.at[i, "hasCashLeg"]) and pd.notna(ev.at[i, "cashPerShare"]) else np.nan
        succ = int(ev.at[i, "successorTradingItemId"]) if bool(ev.at[i, "hasStockLeg"]) and pd.notna(ev.at[i, "successorTradingItemId"]) else None
        ratio = float(ev.at[i, "successorRatio"]) if succ is not None and pd.notna(ev.at[i, "successorRatio"]) else np.nan
        if succ is not None and not np.isfinite(ratio):
            succ = None
    if tr == "unresolved":
        ev.at[i, "cashPerShare"] = np.nan; ev.at[i, "cashPerShareCache"] = np.nan; ev.at[i, "hasCashLeg"] = False
        ev.at[i, "successorTradingItemId"] = pd.NA; ev.at[i, "successorRatio"] = np.nan; ev.at[i, "successorRatioCache"] = np.nan; ev.at[i, "hasStockLeg"] = False
        ev.at[i, "settlementReturn"] = np.nan; ev.at[i, "suggestedReturn"] = np.nan; ev.at[i, "cacheUnitStatus"] = "no_terms"
        ev.at[i, "reviewFlag"] = ((str(ev.at[i, "reviewFlag"]) + ";") if isinstance(ev.at[i, "reviewFlag"], str) and ev.at[i, "reviewFlag"] else "") + "rc2f_unresolved"
    if eff in ("settle_cash", "convert_successor", "settle_mixed", "last_close", "extend_prices"):
        ev.at[i, "cashPerShare"] = cash
        ev.at[i, "cashPerShareCache"] = cash
        ev.at[i, "hasCashLeg"] = bool(np.isfinite(cash) and cash > 0)
        ev.at[i, "successorTradingItemId"] = succ if succ is not None else pd.NA
        ev.at[i, "successorRatio"] = ratio
        ev.at[i, "successorRatioCache"] = ratio
        ev.at[i, "hasStockLeg"] = succ is not None
        succ_close, succ_d = np.nan, pd.NaT
        if succ is not None:
            c, d = closes.on_or_after(np.array([succ]), np.array([np.datetime64(settle_bar, "ns")]), 7)
            succ_close, succ_d = float(c[0]), pd.Timestamp(d[0])
        ev.at[i, "successorCacheClose"] = succ_close
        ev.at[i, "successorCacheCloseDate"] = succ_d
        ev.at[i, "cacheUnitFactorSuccessor"] = 1.0 if np.isfinite(succ_close) else np.nan
        ev.at[i, "acquirerPriceDate"] = succ_d
        if eff == "last_close" or (eff == "extend_prices" and not np.isfinite(cash) and succ is None):
            R, status = np.nan, ("last_close" if eff == "last_close" else "no_terms")   # engine 0 = last-close liquidation (lead decision)
        else:
            proceeds = (cash if np.isfinite(cash) else 0.0) + ((ratio * succ_close) if succ is not None else 0.0)
            if np.isfinite(mark) and mark > 0 and np.isfinite(proceeds):
                R, status = proceeds / mark - 1.0, "ok"
            else:
                R, status = np.nan, ("no_cache_successor_at_settlement" if succ is not None and not np.isfinite(succ_close) else "no_cache_target")
        ev.at[i, "settlementReturn"] = R
        ev.at[i, "suggestedReturn"] = R
        ev.at[i, "cacheUnitStatus"] = status
        if "settlementReturnScope" in ev.columns:
            ev.at[i, "settlementReturnScope"] = "full"
    ev.at[i, "source"] = "rc2f_override"
    ev.at[i, "confidence"] = "override"
    ev.at[i, "actionText"] = (f"[rc2f {tr}{'->' + eff if eff != tr else ''}/{rec['endReason']}] " + str(rec["evidence"]))[:200]
    ev.at[i, "reviewFlag"] = ((str(ev.at[i, "reviewFlag"]) + ";") if isinstance(ev.at[i, "reviewFlag"], str) and ev.at[i, "reviewFlag"] else "") + f"rc2f_override:{tr}" + (f"->{eff}" if eff != tr else "")
    what = f"endDate {pd.Timestamp(end).date()} settle {pd.Timestamp(ev.at[i, 'settlementDate']).date()} {tr}{'->' + eff if eff != tr else ''} mark {mark:g}@{mark_d.date() if pd.notna(mark_d) else None}"
    what += f" cash {cash} succ {succ} ratio {ratio} R {ev.at[i, 'settlementReturn']}"
    return ev, ("events row created: " if created else "events row updated: ") + what


def _events_types(ev: pd.DataFrame) -> pd.DataFrame:
    ev = ev.copy()
    for c in ("endDate", "settlementDate", "endDateOriginal", "acquirerPriceDate", "targetCacheMarkDate", "cacheUnitFactorTargetDate", "successorCacheCloseDate"):
        ev[c] = pd.to_datetime(ev[c]).astype("datetime64[ns]")
    ev["successorTradingItemId"] = pd.array(pd.to_numeric(ev.successorTradingItemId, errors="coerce"), dtype="Int64")
    for c in ("endDateConflict", "hasCashLeg", "hasStockLeg"):
        ev[c] = ev[c].astype(bool)
    ev["endDateGapDays"] = pd.to_numeric(ev.endDateGapDays).fillna(0).astype("int32")
    for c in ("settlementReturn", "suggestedReturn", "cashPerShare", "successorRatio", "cashPerShareCache", "successorRatioCache", "cacheUnitFactorTarget",
              "cacheUnitFactorSuccessor", "targetCacheMark", "successorCacheClose"):
        ev[c] = pd.to_numeric(ev[c]).astype("float64")
    return ev.sort_values(["endDate", "tradingItemId"]).reset_index(drop=True)


def apply_lifecycle_overrides(df: pd.DataFrame, lc: pd.DataFrame, lc_aux: pd.DataFrame, dates: pd.DataFrame, ev_reclass: pd.DataFrame, ev_rc1: pd.DataFrame,
                              ln: pd.DataFrame, closes, cal, alog: ApplyLog, top500: set, cal_days: np.ndarray | None = None, lineage_touched: set | None = None,
                              lc_all_first: dict | None = None) -> dict:
    """반환 {'lifecycle', 'lifecycle_aux', 'lifecycle_dates', 'events_reclass', 'events_rc1', 'lineage', 'changed': set(...)}.
    바뀌는 것: lifecycle(.parquet/aux): lastTradeDate·lastTradeSource='rc2f_override'·endReason(LC_END_REASON 매핑)·status('confirmed' 종료 시)·evidence.
    lifecycle_dates: lastTradeDate/effectiveDate/settlementDate/payDate + *Source='rc2f_override:<id>', eventEndDate=events.endDate, actionCode, gap·mismatchClass 재계산.
    events(R3 reclass + R1 root 의 rc1 events 사본): _patch_events. lineage: convert_successor/settle_mixed 는 merger 링크(oldId=tid, newId=successor, ratio) 추가/갱신."""
    from master_aux import DATES_COLS
    lc, lc_aux, dates, ln = lc.copy(), lc_aux.copy() if lc_aux is not None else None, dates.copy(), ln.copy()
    ev_reclass, ev_rc1 = ev_reclass.copy(), ev_rc1.copy()
    changed = set()
    lineage_touched = lineage_touched or set()
    if cal_days is None:
        cal_days = np.asarray(cal.days).astype("datetime64[ns]")
    if lc_all_first is None:
        lc_all_first = {int(t): f for t, f in zip(lc.index, lc.firstTradeDate)}
        if lc_aux is not None:
            lc_all_first.update({int(t): f for t, f in zip(lc_aux.index, lc_aux.firstTradeDate)})
    for r in df.itertuples(index=False):
        tid, oid = int(r.x_tid), r.overrideId
        rec = dict(lastTradeDate=parse_date(r.lastTradeDate), effectiveDate=parse_date(r.effectiveDate), settlementDate=parse_date(r.settlementDate),
                   payDate=parse_date(r.payDate), endReason=r.endReason.strip(), successor=parse_int(r.successorTradingItemId), successorRatio=parse_float(r.successorRatio),
                   cashPerShare=parse_float(r.cashPerShare), treatment=r.treatment.strip(), evidence=r.evidence, lifecycleLast=pd.NaT)
        tr = rec["treatment"]
        prev_reason = alog.rows.get(oid, {}).get("reason", "") if tr == "extend_prices" else ""
        prev_rows = alog.rows.get(oid, {}).get("affected_rows", 0) if tr == "extend_prices" else 0
        alog.set(oid, caseId=r.caseId, kind="lifecycle", action=tr, target=str(tid), file="lifecycle")
        tab = lc if tid in lc.index else (lc_aux if lc_aux is not None and tid in lc_aux.index else None)
        if tab is None:
            alog.set(oid, status="rejected", reason="id_not_in_lifecycle", affected_rows=0)
            continue
        if rec["successor"] is not None and (rec["successorRatio"] is None or not np.isfinite(rec["successorRatio"]) or rec["successorRatio"] <= 0):
            # R-X4 re-review guard (the validator rejects this first; kept so a bypassed validate cannot reach ratio x close in _patch_events)
            alog.set(oid, status="rejected", reason=f"successor_without_ratio: successorTradingItemId {rec['successor']} needs a positive successorRatio (treatment {tr})", affected_rows=0)
            continue
        rec["lifecycleLast"] = pd.to_datetime(tab.at[tid, "lastTradeDate"])
        n = int(prev_rows or 0)
        notes = [prev_reason] if prev_reason else []
        # 1) lifecycle
        if rec["lastTradeDate"] is not None:
            before = tab.at[tid, "lastTradeDate"]
            tab.at[tid, "lastTradeDate"] = rec["lastTradeDate"]
            tab.at[tid, "lastTradeSource"] = "rc2f_override"
            n += 1
            notes.append(f"lifecycle.lastTradeDate {pd.Timestamp(before).date() if pd.notna(before) else None} -> {rec['lastTradeDate'].date()}")
        new_reason = LC_END_REASON[rec["endReason"]]
        if str(tab.at[tid, "endReason"]) != new_reason:
            notes.append(f"lifecycle.endReason {tab.at[tid, 'endReason']} -> {new_reason}")
            tab.at[tid, "endReason"] = new_reason
            n += 1
        eff_tr = tr
        if tr == "extend_prices":
            eff_tr = "settle_mixed" if (rec["cashPerShare"] is not None and rec["successor"] is not None) else ("settle_cash" if rec["cashPerShare"] is not None else ("convert_successor" if rec["successor"] is not None else "extend_prices"))
        if eff_tr in ("settle_cash", "convert_successor", "settle_mixed", "last_close") and str(tab.at[tid, "status"]) == "active":
            tab.at[tid, "status"] = "confirmed"
            notes.append("lifecycle.status active -> confirmed")
            n += 1
        tab.at[tid, "evidence"] = (str(tab.at[tid, "evidence"]) + f" | rc2f:{oid}:{tr}:{rec['endReason']} " + str(rec["evidence"]))[:600]
        changed.add("lifecycle" if tab is lc else "lifecycle_aux")
        # 2) events (R3 reclass + rc1 root copy)
        ev_reclass, what = _patch_events(ev_reclass, tid, rec, closes, cal, oid)
        if what.startswith("reject"):
            alog.set(oid, status="rejected", reason=what, affected_rows=0)
            continue
        ev_rc1, _ = _patch_events(ev_rc1, tid, rec, closes, cal, oid)
        notes.append(what)
        changed |= {"events_reclass", "events_rc1"}
        n += 1
        # 3) lifecycle_dates
        has_ev = tid in ev_reclass.tradingItemId.values
        if tid in dates.index or has_ev:
            if tid not in dates.index:
                new = {c: None for c in DATES_COLS if c != "tradingItemId"}
                new.update({"lastTradeDate": rec["lifecycleLast"], "lastTradeSource": str(tab.at[tid, "lastTradeSource"]), "effectiveDate": pd.NaT, "effectiveDateSource": "",
                            "settlementDate": pd.NaT, "settlementDateSource": "", "payDate": pd.NaT, "payDateSource": "",
                            "securityEndDate": tab.at[tid, "securityEndDate"] if "securityEndDate" in tab.columns else pd.NaT,
                            "ciqLastCloseDate": tab.at[tid, "ciqLastCloseDate"] if "ciqLastCloseDate" in tab.columns else pd.NaT,
                            "miLastCloseDate": tab.at[tid, "miLastCloseDate"] if "miLastCloseDate" in tab.columns else pd.NaT,
                            "eventEndDate": pd.NaT, "actionCode": None, "gapEffectiveMinusLastTrade": pd.NA, "gapSettlementMinusLastTrade": pd.NA,
                            "mismatchClass": "match", "everTop500": tid in top500})
                dates.loc[tid] = pd.Series(new)
                notes.append("lifecycle_dates row created")
            d = dates.loc[tid]
            src = f"rc2f_override:{oid}"
            if rec["lastTradeDate"] is not None:
                dates.at[tid, "lastTradeDate"] = rec["lastTradeDate"]; dates.at[tid, "lastTradeSource"] = src
            for c in ("effectiveDate", "settlementDate", "payDate"):
                if rec[c] is not None:
                    dates.at[tid, c] = rec[c]; dates.at[tid, c + "Source"] = src
            if tr == "unresolved":
                dates.at[tid, "settlementDate"] = pd.NaT; dates.at[tid, "settlementDateSource"] = src + "(unresolved: none)"
            if has_ev:
                e = ev_reclass[ev_reclass.tradingItemId == tid].iloc[0]
                dates.at[tid, "eventEndDate"] = e.endDate
                dates.at[tid, "actionCode"] = e.actionCode
                if rec["effectiveDate"] is None and pd.isna(dates.at[tid, "effectiveDate"]):
                    dates.at[tid, "effectiveDate"] = e.endDateOriginal; dates.at[tid, "effectiveDateSource"] = "override"
                if rec["settlementDate"] is None and rec["payDate"] is None and pd.isna(dates.at[tid, "settlementDate"]) and tr != "unresolved":
                    dates.at[tid, "settlementDate"] = e.settlementDate; dates.at[tid, "settlementDateSource"] = src + "(next trading day after endDate)"
                if rec["payDate"] is None and eff_tr in ("settle_cash", "convert_successor", "settle_mixed"):
                    dates.at[tid, "payDate"] = e.settlementDate; dates.at[tid, "payDateSource"] = src + "(events.settlementDate = ledger merger payDate)"
            lt = pd.to_datetime(dates.at[tid, "lastTradeDate"])
            eff, sd = pd.to_datetime(dates.at[tid, "effectiveDate"]), pd.to_datetime(dates.at[tid, "settlementDate"])
            dates.at[tid, "gapEffectiveMinusLastTrade"] = (eff - lt).days if pd.notna(eff) and pd.notna(lt) else pd.NA
            dates.at[tid, "gapSettlementMinusLastTrade"] = (sd - lt).days if pd.notna(sd) and pd.notna(lt) else pd.NA
            ee = pd.to_datetime(dates.at[tid, "eventEndDate"])
            ge = (ee - lt).days if pd.notna(ee) and pd.notna(lt) else 0
            dates.at[tid, "mismatchClass"] = "endDate_after_lastTrade" if ge > 0 else ("endDate_before_lastTrade" if ge < 0 else "match")
            changed.add("lifecycle_dates")
            n += 1
        # 4) lineage merger link (convert/mixed, incl. extend_prices rows carrying a successor)
        if eff_tr in ("convert_successor", "settle_mixed") and rec["successor"] is not None:
            m = (ln.oldId == tid) & (ln.newId == rec["successor"])
            eff = rec["settlementDate"] or rec["payDate"] or rec["effectiveDate"] or rec["lastTradeDate"] or rec["lifecycleLast"]
            if m.any():
                i = ln.index[m][0]
                ln.at[i, "ratio"] = rec["successorRatio"]; ln.at[i, "ratioSource"] = "rc2f_override"; ln.at[i, "effectiveDate"] = pd.Timestamp(eff)
                ln.at[i, "evidence"] = (str(ln.at[i, "evidence"]) + f" | rc2f:{oid}: " + str(rec["evidence"]))[:600]; ln.at[i, "version"] = "rc2f"
                notes.append(f"lineage merger link ratio -> {rec['successorRatio']}")
            else:
                row = {"oldId": tid, "newId": rec["successor"], "relation": "merger", "effectiveDate": pd.Timestamp(eff), "ratio": rec["successorRatio"],
                       "evidence": (f"rc2f:{oid}: " + str(rec["evidence"]))[:600], "confidence": "high", "ratioSource": "rc2f_override", "linkClass": "merger",
                       "reviewFlag": "", "sources": "rc2f", "version": "rc2f", "chainGapTradingDays": pd.NA, "chainable": False,
                       "linkVerified": False}                      # rc2f-E column (present once lineage overrides ran)
                ln = pd.concat([ln, pd.DataFrame([{k: row.get(k, pd.NA) for k in ln.columns}])], ignore_index=True)
                notes.append("lineage merger link added")
            changed.add("lineage")
            n += 1
        # 5) extended lastTradeDate -> recompute chainGapTradingDays of this id's rename/relist links (R-X4 §4-3); chainable follows the R3 formula
        #    only for rows not touched by a lineage override of this run (those keep the override's chainable)
        if rec["lastTradeDate"] is not None:
            import master_lifecycle as ml
            first_map = lc_all_first if lc_all_first is not None else {}
            for j in ln.index[(ln.oldId == tid) & ln.relation.isin(["rename", "relist"])]:
                new_id = int(ln.at[j, "newId"])
                fn = pd.to_datetime(first_map.get(new_id, pd.NaT))
                if pd.isna(fn):
                    continue
                gap = 0 if fn <= rec["lastTradeDate"] else int(ml.tdays_between(cal_days, np.array([rec["lastTradeDate"]], dtype="datetime64[ns]"), np.array([fn], dtype="datetime64[ns]"))[0]) - 1
                before_gap = ln.at[j, "chainGapTradingDays"]
                ln.at[j, "chainGapTradingDays"] = gap
                if (int(ln.at[j, "oldId"]), new_id) not in lineage_touched and not bool(ln.at[j, "chainable"]):
                    formula = str(ln.at[j, "confidence"]) in ("high", "medium") and pd.notna(ln.at[j, "ratio"]) and gap <= MAX_CHAIN_GAP
                    if formula:
                        ln.at[j, "chainable"] = True
                        notes.append(f"lineage {tid}->{new_id} chainGap {before_gap}->{gap}: chainable True (R3 formula)")
                elif before_gap != gap:
                    notes.append(f"lineage {tid}->{new_id} chainGap {before_gap}->{gap}")
        alog.set(oid, status="applied", reason="; ".join(notes), affected_rows=n)
    ln["chainGapTradingDays"] = ln.chainGapTradingDays.astype("Int64")
    ln["chainable"] = ln.chainable.astype(bool)
    ln["effectiveDate"] = pd.to_datetime(ln.effectiveDate).astype("datetime64[ns]")
    dates = dates.reset_index().rename(columns={"index": "tradingItemId"}) if "tradingItemId" not in dates.columns else dates.reset_index(drop=True)
    for c in ("lastTradeDate", "effectiveDate", "settlementDate", "payDate", "securityEndDate", "ciqLastCloseDate", "miLastCloseDate", "eventEndDate"):
        dates[c] = pd.to_datetime(dates[c]).astype("datetime64[ns]")
    for c in ("gapEffectiveMinusLastTrade", "gapSettlementMinusLastTrade"):
        dates[c] = dates[c].astype("Int64")
    dates["everTop500"] = dates.everTop500.astype(bool)
    dates["tradingItemId"] = dates.tradingItemId.astype("int64")
    return {"lifecycle": lc.reset_index(), "lifecycle_aux": lc_aux.reset_index() if lc_aux is not None else None, "lifecycle_dates": dates[DATES_COLS].sort_values("tradingItemId").reset_index(drop=True),
            "events_reclass": _events_types(ev_reclass), "events_rc1": _events_types(ev_rc1), "lineage": ln.sort_values(["oldId", "newId"]).reset_index(drop=True), "changed": changed}


# --------------------------------------------------------------------------------------
# R-X4 (d): settlementReturnScope labels (the rc2f settlement-cash rule RULE-X3-5 was removed, R-X0 P0-1)
# --------------------------------------------------------------------------------------
def label_settlement_scope(ev: pd.DataFrame, ledger: pd.DataFrame, lifecycle: pd.DataFrame, cal, alog: ApplyLog) -> tuple[pd.DataFrame, pd.DataFrame, int]:
    """R-X4 §4 (d) `settlementReturnScope` 규약(리드 결정): full = events 행이 기록된 대가 전부를 보유; events_legs_only = 대가의 일부가 원장에 있음
    (정산 bar 의 원장 value leg = 2차 자회사/승계 leg, 또는 RULE-X3-5 로 원장에 넘긴 현금 leg) → settlementReturn 은 events leg 만의 partial R;
    partial_terms = R3 규약 그대로(가격 없는 leg → R NaN). treatment 별 기본: settle_cash/convert_successor/settle_mixed/last_close/unresolved/extend_prices 모두
    full 로 쓰되, 원장에 정산 bar(lifecycle lastTradeDate 다음 거래일)~+7일 사이의 **엔진 export 대상** value leg 가 있으면 events_legs_only 로 바꾸고
    reviewFlag 에 `multi_leg_split_path(child legs in ledger)` 를 붙인다(R3 13행과 같은 표기). export 대상 = loader.export_ca_ledger 규칙(eventType ∉
    CA_EXPORT_EXCLUDED_TYPES(merger_stock 등), source ≠ stitched_copy, 자회사 id 있음, events 승계사 stock leg 가 아님; R-X0 2차 P2-15 — merger_stock 행은
    export 되지 않으므로 라벨 근거가 아님). 정산 bar 이후에 남는 value leg 는 경고 목록으로 반환."""
    from loader import CA_EXPORT_EXCLUDED_TYPES
    ev = ev.copy()
    lc = lifecycle.set_index("tradingItemId")
    last = lc.lastTradeDate.copy()
    if "status" in lc.columns:
        last[lc.status == "active"] = pd.NaT
    legs = ledger[ledger.legType.isin(["stock", "rights"]) & ~ledger.source.isin(["corporate_actions", "stitched_copy"])
                  & ~ledger.eventType.isin(list(CA_EXPORT_EXCLUDED_TYPES)) & ledger.childTradingItemId.notna()]
    if "exportExclude" in legs.columns:
        legs = legs[~legs.exportExclude.fillna(False).astype(bool)]
    ev_ids = set(int(x) for x in ev[(ev.hasCashLeg | ev.hasStockLeg)].tradingItemId)
    legs = legs[legs.tradingItemId.astype(int).isin(list(ev_ids))]
    ev_succ = {int(t): (None if pd.isna(sx) else int(sx)) for t, sx in zip(ev.tradingItemId, ev.successorTradingItemId)} if "successorTradingItemId" in ev.columns else {}
    if len(legs) and ev_succ:      # the events successor stock leg on/after settlement is excluded from the export too (loader E1 §9)
        succ_leg = np.array([ev_succ.get(int(t)) is not None and int(c) == ev_succ.get(int(t)) for t, c in zip(legs.tradingItemId, legs.childTradingItemId)])
        legs = legs[~succ_leg]
    n_relabel = 0
    warn_rows = []
    by_ev = ev.set_index("tradingItemId")
    for tid, g in legs.groupby(legs.tradingItemId.astype(int)):
        ltd = last.get(tid, pd.NaT)
        if pd.isna(ltd):
            continue
        bar = pd.Timestamp(cal.align(np.array([np.datetime64(pd.Timestamp(ltd) + pd.Timedelta(days=1), "ns")]))[0][0])
        ex = pd.to_datetime(g.exDate)
        on_bar = g[(ex >= bar) & (ex <= bar + pd.Timedelta(days=7))]
        after = g[ex > bar]
        for r in after.itertuples(index=False):
            warn_rows.append({"tradingItemId": tid, "eventId": r.eventId, "exDate": pd.Timestamp(r.exDate).date(), "settlementBar": bar.date(), "lastTradeDate": pd.Timestamp(ltd).date(),
                              "childTradingItemId": r.childTradingItemId, "childRatio": r.childRatio, "status": r.status, "note": "value leg after the engine settlement bar: lost in the engine (RULE-X4-2 aligns legs within 7 days; later legs need an exDate fix)"})
        if len(on_bar) and tid in by_ev.index:
            i = ev.index[ev.tradingItemId == tid][0]
            if str(ev.at[i, "settlementReturnScope"]) == "full":
                ev.at[i, "settlementReturnScope"] = "events_legs_only"
                flag = str(ev.at[i, "reviewFlag"]) if isinstance(ev.at[i, "reviewFlag"], str) else ""
                if "multi_leg_split_path" not in flag:
                    ev.at[i, "reviewFlag"] = (flag + ";" if flag else "") + "multi_leg_split_path(child legs in ledger):" + ",".join(f"{int(c)}x{r:g}" for c, r in zip(on_bar.childTradingItemId.fillna(-1), on_bar.childRatio.fillna(float("nan"))))
                alog.set(f"RULE-X4-scope:{tid}", kind="rule", action="settlementReturnScope", status="applied", affected_rows=1, target=str(tid), file="events",
                         reason=f"full -> events_legs_only: {len(on_bar)} ledger value leg(s) on the settlement bar {bar.date()} ({','.join(on_bar.eventId)})")
                n_relabel += 1
    return ev, pd.DataFrame(warn_rows), n_relabel


# --------------------------------------------------------------------------------------
# aux_additions.csv (X3 (h)): collect prices/identity/lifecycle/factors/dividends of extra auxiliary ids (xfeed read-only, chunks <= 1000)
# --------------------------------------------------------------------------------------
def apply_aux_additions(df: pd.DataFrame, r3s: Path, out_r3: Path, alog: ApplyLog, ref: Ref, asof: str, work: Path, ledger_ref: pd.DataFrame | None) -> dict:
    """Adds ids to R3 aux: master/aux_instruments.parquet (+row), base_data_aux.parquet (+prices from ciqpriceequity), master/lifecycle_aux.parquet,
    factors_aux/{split,dividend}.parquet (+intervals), for_R1_events.csv (+ciqdividend cash rows, source ciqdividend(aux)). Ids already in
    base_data/master or aux -> no_op. Non-USD / no identity -> rejected (same rule as master_aux)."""
    import master_aux as ma
    import master_build as mb
    import prices_build as pb
    import prices_factors as pf
    from db import DB
    aux_old = pd.read_parquet(r3s / "master" / "aux_instruments.parquet")
    have = set(int(i) for i in aux_old.tradingItemId)
    todo = {}
    for r in df.itertuples(index=False):
        tid = int(r.x_tid)
        alog.set(r.overrideId, caseId=r.caseId, kind="aux", action="add", target=str(tid), file="aux")
        if tid in have or tid in ref.ids:
            alog.set(r.overrideId, status="no_op", reason="already in aux/base_data", affected_rows=0)
            continue
        if tid in todo:
            alog.set(r.overrideId, status="no_op", reason=f"duplicate aux addition (handled by {todo[tid]})", affected_rows=0)
            continue
        todo[tid] = r.overrideId
    if not todo:
        return {"added": 0}
    ids = sorted(todo)
    work.mkdir(parents=True, exist_ok=True)
    db = DB(application_name="cache_v21_rc2f_aux")
    try:
        ident = ma.identity(db, ids)
        # referencing date: earliest ledger leg naming the id as child (else 2000-01-01)
        need = {}
        if ledger_ref is not None:
            ch = ledger_ref[ledger_ref.childTradingItemId.astype("Int64").isin(ids)]
            need = ch.groupby(ch.childTradingItemId.astype("Int64")).exDate.min().to_dict()
        refs = pd.DataFrame({"tradingItemId": ids, "date": [pd.Timestamp(need.get(i, pd.Timestamp("2000-01-01"))) for i in ids],
                             "reason": "rc2f_aux_addition", "ref": [todo[i] for i in ids]})
        aux_new = ma.build_aux_instruments(refs, ident)
        usd = sorted(int(i) for i in aux_new.loc[aux_new.priceFetch == "usd", "tradingItemId"])
        for r in aux_new.itertuples(index=False):
            if r.priceFetch != "usd":
                alog.set(todo[int(r.tradingItemId)], status="rejected", reason=f"priceFetch={r.priceFetch}", affected_rows=0)
        info = {"identity": int(len(ident)), "usd": len(usd)}
        if usd:
            pinfo = pb.build_ids(db, usd, work / "base_data_aux_new.parquet", work, "2000-01-01", asof, 1000, label="rc2f_aux")
            cov = pinfo["coverage"]
            cov.to_parquet(work / "base_data_aux_new_coverage.parquet", index=False)
            with_rows = sorted(int(i) for i in cov[cov.nRows > 0].tradingItemId)
            finfo = pf.build_all(db, with_rows, work / "factors_stage", work) if with_rows else None
            div_rows = ma.aux_dividend_rows(db, with_rows) if with_rows else pd.DataFrame()
            info.update(rows=int(pinfo["rows"]), ids_with_rows=len(with_rows), dividend_rows=int(len(div_rows)))
        else:
            cov, with_rows, finfo, div_rows = pd.DataFrame(columns=["tradingItemId", "nRows", "nCloseRows", "firstCloseDate", "lastCloseDate"]), [], None, pd.DataFrame()
    finally:
        db.close()
    cal = pd.read_parquet(ref.rc2 / "calendar.parquet")
    cal_days = cal.loc[cal.isTradingDay, "date"].to_numpy(dtype="datetime64[ns]")
    # ---- merge into the build's R3 copies
    out_r3.mkdir(parents=True, exist_ok=True); (out_r3 / "master").mkdir(exist_ok=True); (out_r3 / "factors_aux").mkdir(exist_ok=True)
    aux_all = pd.concat([aux_old, aux_new[aux_old.columns]], ignore_index=True).sort_values("tradingItemId").reset_index(drop=True)
    for c in ("exchangeId", "tiCurrencyId", "securitySubTypeId", "tradingItemStatusId"):
        aux_all[c] = aux_all[c].astype("Int64")
    _write_parquet_like(aux_all, r3s / "master" / "aux_instruments.parquet", out_r3 / "master" / "aux_instruments.parquet")
    lc_new = ma.aux_lifecycle(aux_new[aux_new.priceFetch == "usd"].reset_index(drop=True), cov, pd.Timestamp(asof), pd.Timestamp(mb.now()).normalize()) if usd else pd.DataFrame()
    lc_old = pd.read_parquet(r3s / "master" / "lifecycle_aux.parquet")
    if len(lc_new):
        lc_all = pd.concat([lc_old, lc_new[lc_old.columns]], ignore_index=True).sort_values("tradingItemId").reset_index(drop=True)
        _write_parquet_like(lc_all, r3s / "master" / "lifecycle_aux.parquet", out_r3 / "master" / "lifecycle_aux.parquet")
    if with_rows:
        old_px = r3s / "base_data_aux.parquet"
        src_px = out_r3 / "base_data_aux.parquet"
        base_px = src_px if (src_px.exists() and not src_px.is_symlink()) else old_px          # price overrides may have rewritten it already
        a = pd.read_parquet(base_px); b = pd.read_parquet(work / "base_data_aux_new.parquet")
        merged = pd.concat([a, b[a.columns]], ignore_index=True).sort_values(["tradingItemId", "date"], kind="mergesort").reset_index(drop=True)
        schema = pq.ParquetFile(old_px).schema_arrow
        schema = pa.schema([schema.field(i) for i in range(len(schema.names))])
        tmp = out_r3 / "base_data_aux.parquet.tmp"
        pq.write_table(pa.Table.from_pandas(merged[schema.names], schema=schema, preserve_index=False), tmp, compression="zstd")
        if src_px.is_symlink() or src_px.exists():
            src_px.unlink()
        os.replace(tmp, src_px)
        for name in ("split", "dividend"):
            f_old = pd.read_parquet(r3s / "factors_aux" / f"{name}.parquet")
            f_new = pd.read_parquet(work / "factors_stage" / "factors" / f"{name}.parquet")
            f_all = pd.concat([f_old, f_new[f_old.columns]], ignore_index=True).sort_values(["tradingItemId", "validFrom"]).reset_index(drop=True)
            chk = pf.check_intervals(f_all)
            if not chk["ok"]:
                raise SystemExit(f"aux factors_aux/{name} broke the interval contract: {chk['violations']}")
            dst = out_r3 / "factors_aux" / f"{name}.parquet"
            if dst.is_symlink() or dst.exists():
                dst.unlink()
            _write_parquet_like(f_all, r3s / "factors_aux" / f"{name}.parquet", dst)
        if len(div_rows):
            tgt = out_r3 / "for_R1_events.csv"
            base = pd.read_csv(tgt if (tgt.exists() and not tgt.is_symlink()) else r3s / "for_R1_events.csv")
            merged_ev = pd.concat([base, div_rows.assign(exDate=pd.to_datetime(div_rows.exDate).dt.strftime("%Y-%m-%d"))], ignore_index=True)
            if tgt.is_symlink():
                tgt.unlink()
            merged_ev.to_csv(tgt, index=False)
    covm = cov.set_index("tradingItemId") if len(cov) else None
    for tid, oid in todo.items():
        if alog.rows[oid]["status"] in ("rejected", "no_op"):
            continue
        n_rows = int(covm.nRows.get(tid, 0)) if covm is not None and tid in covm.index else 0
        alog.set(oid, status="applied" if n_rows else "rejected", reason=(f"aux added: {n_rows} price rows, first close {covm.firstCloseDate.get(tid)} last {covm.lastCloseDate.get(tid)}" if n_rows else "no ciqpriceequity rows"), affected_rows=n_rows)
    return info


# --------------------------------------------------------------------------------------
# lifecycle extend_prices (X4 인계): (기존 lastTradeDate, 새 lastTradeDate] 구간 가격을 xfeed 에서 보충해 base_data 에 append
# --------------------------------------------------------------------------------------
def supplement_prices(db, tid: int, old_last, new_last, existing_days: set, cal_days: np.ndarray) -> tuple[pd.DataFrame, dict]:
    """(old_last, new_last] 의 거래일 중 base_data 에 없는 날짜를 ciqpriceequity(raw = price x adjustmentfactor, source 1) 로, 그래도 없으면 miprice
    (close/volume 만, open/high/low/vwap NaN, source 2) 로 채운 BASE_SCHEMA 행. qualityFlag = QF_SUPPLEMENTED. close 없는 행은 제외."""
    import prices_build as pb
    lo, hi = pd.Timestamp(old_last) + pd.Timedelta(days=1), pd.Timestamp(new_last)
    need = set(int(d) for d in np.asarray(cal_days).astype("datetime64[D]").astype(np.int64) if lo <= pd.Timestamp(np.datetime64(int(d), "D")) <= hi) - existing_days
    info = {"trading_days_needed": len(need), "existing_rows": 0, "ciq_rows": 0, "mi_rows": 0}
    if not need:
        return pd.DataFrame(), info
    frames = []
    t = pb.fetch_ciq(db, [tid], str(lo.date()), str(hi.date()))
    if t.num_rows:
        base, _ = pb.transform_chunk(t, str(hi.date()))
        b = base.to_pandas()
        b = b[np.isfinite(b.close.to_numpy(np.float64))]
        b = b[b.date.map(lambda d: int(np.datetime64(d, "D").astype(np.int64))).isin(need)]
        if len(b):
            b["qualityFlag"] = np.int8(QF_SUPPLEMENTED); b["source"] = np.int8(SOURCE_CIQ)
            frames.append(b); need -= set(b.date.map(lambda d: int(np.datetime64(d, "D").astype(np.int64))))
            info["ciq_rows"] = int(len(b))
    if need:
        m = pb.fetch_mi(db, [tid], str(lo.date()), str(hi.date()))
        if m.num_rows:
            md = m.to_pandas()
            md["date"] = pd.to_datetime(md.pricedate).astype("datetime64[ns]")
            md = md[np.isfinite(md.priceclose.to_numpy(np.float64))]
            md = md[md.date.map(lambda d: int(np.datetime64(d, "D").astype(np.int64))).isin(need)]
            if len(md):
                b = pd.DataFrame({"date": md.date.to_numpy(), "tradingItemId": np.int64(tid), "open": np.float32(np.nan), "high": np.float32(np.nan), "low": np.float32(np.nan),
                                  "close": md.priceclose.astype(np.float32).to_numpy(), "vwap": np.float32(np.nan), "volume": md.volume.astype(np.float64).to_numpy(),
                                  "source": np.int8(SOURCE_MI), "qualityFlag": np.int8(QF_SUPPLEMENTED)})
                frames.append(b); info["mi_rows"] = int(len(b))
    out = pd.concat(frames, ignore_index=True).sort_values("date").reset_index(drop=True) if frames else pd.DataFrame()
    return out, info


def apply_extend_prices(df: pd.DataFrame, lc_all: pd.DataFrame, t2_base_dst: Path, aux_dst: Path, t2_base_src: Path, aux_src: Path, aux_ids: set,
                        alog: ApplyLog, work: Path, cal_days: np.ndarray) -> dict:
    """lifecycle_overrides 의 treatment extend_prices 행: (기존 lifecycle.lastTradeDate, 새 lastTradeDate] 의 빠진 거래일 가격을 xfeed 에서 보충해 T2/aux
    base_data 에 append(master_aux.merge_base_data 로 (id, date) 전역 정렬 유지). lifecycle/lifecycle_dates/events 는 이어지는 lifecycle 단계가 새 lastTradeDate
    로 맞추고, returns 는 publish 가 보충된 base 로 재계산·마스킹한다. 로그 affected_rows = 추가된 행."""
    from db import DB
    from master_aux import merge_base_data
    recs = df[df.treatment.str.strip() == "extend_prices"]
    if not len(recs):
        return {"overrides": 0}
    lc = lc_all.set_index("tradingItemId")
    new_rows = {"T2": [], "aux": []}
    db = DB(application_name="cache_v21_rc2f_extend")
    try:
        for r in recs.itertuples(index=False):
            tid, oid = int(r.x_tid), r.overrideId
            new_last = parse_date(r.lastTradeDate)
            old_last = pd.to_datetime(lc.at[tid, "lastTradeDate"]) if tid in lc.index else pd.NaT
            if new_last is None or pd.isna(old_last) or new_last <= old_last:
                alog.set(oid, caseId=r.caseId, kind="lifecycle", action="extend_prices", status="rejected", reason=f"lastTradeDate {new_last} not after lifecycle {old_last}", affected_rows=0, target=str(tid), file="extend")
                continue
            label = "aux" if tid in aux_ids else "T2"
            src = aux_dst if label == "aux" else t2_base_dst
            ex = pq.read_table(src, columns=["date", "close"], filters=[("tradingItemId", "==", tid), ("date", ">", old_last), ("date", "<=", new_last)]).to_pandas()
            ex = ex[np.isfinite(ex.close.to_numpy(np.float64))]
            existing = set(int(np.datetime64(d, "D").astype(np.int64)) for d in ex.date)
            rows, info = supplement_prices(db, tid, old_last, new_last, existing, cal_days)
            info["existing_rows"] = len(existing)
            if len(rows):
                new_rows[label].append(rows)
            alog.set(oid, caseId=r.caseId, kind="lifecycle", action="extend_prices", status="applied", affected_rows=int(len(rows)), target=f"{tid} ({old_last.date()}, {new_last.date()}]", file=label,
                     reason=f"supplemented {len(rows)} rows (ciq {info['ciq_rows']}, mi {info['mi_rows']}); base already had {len(existing)} of {info['trading_days_needed'] + len(existing)} trading days")
    finally:
        db.close()
    out = {"overrides": int(len(recs)), "rows_added": {}}
    import prices_build as pb
    for label, parts in new_rows.items():
        if not parts:
            continue
        add = pd.concat(parts, ignore_index=True)
        for c in ("open", "high", "low", "close", "vwap"):
            add[c] = add[c].astype(np.float32)
        add["volume"] = add.volume.astype(np.float64); add["source"] = add.source.astype(np.int8); add["qualityFlag"] = add.qualityFlag.astype(np.int8)
        add["tradingItemId"] = add.tradingItemId.astype(np.int64); add["date"] = pd.to_datetime(add.date).astype("datetime64[ns]")
        work.mkdir(parents=True, exist_ok=True)
        tmp_add = work / f"supplement_{label}.parquet"
        pq.write_table(pa.Table.from_pandas(add[[f.name for f in pb.BASE_SCHEMA]], schema=pb.BASE_SCHEMA, preserve_index=False), tmp_add)
        dst = aux_dst if label == "aux" else t2_base_dst
        base_src = dst if (dst.exists() and not dst.is_symlink()) else (aux_src if label == "aux" else t2_base_src)
        tmp_out = dst.with_name(dst.name + ".ext.tmp")
        merge_base_data(base_src, tmp_add, tmp_out)
        if dst.is_symlink() or dst.exists():
            dst.unlink()
        os.replace(tmp_out, dst)
        out["rows_added"][label] = int(len(add))
    return out


# --------------------------------------------------------------------------------------
# apply driver
# --------------------------------------------------------------------------------------
def _write_parquet_like(df: pd.DataFrame, src: Path, dst: Path, extra: dict | None = None) -> None:
    """src 의 arrow 스키마로 df 를 저장(컬럼 순서·dtype 유지). extra {컬럼: pyarrow 타입}: src 에 없지만 df 에 있는 새 컬럼을 뒤에 덧붙여 저장
    (rc2f-E linkVerified 처럼 스키마 확장; 없으면 조용히 버려지는 사고 방지)."""
    schema = pq.ParquetFile(src).schema_arrow
    fields = [schema.field(i) for i in range(len(schema.names))]                    # pandas 메타데이터 제거
    for c, typ in (extra or {}).items():
        if c in df.columns and c not in schema.names:
            fields.append(pa.field(c, typ))
    schema = pa.schema(fields)
    t = pa.Table.from_pandas(df[schema.names], schema=schema, preserve_index=False)
    dst.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(t, dst, compression="zstd" if src.suffix == ".parquet" and pq.ParquetFile(src).metadata.row_group(0).column(0).compression == "ZSTD" else "snappy")


def cmd_apply(args) -> int:
    t0 = time.time()
    rc2, stage_rc2, stage_v21, out = Path(args.rc2), Path(args.stage_rc2), Path(args.stage_v21), Path(args.out)
    r1s, r3s = stage_rc2 / "R1", stage_rc2 / "R3"
    if out.exists() and not args.force:
        raise SystemExit(f"{out} exists (use --force to rebuild)")
    if out.exists():                      # logs/ 는 보존(rc2f_build.sh 가 이 명령의 로그를 그 안에 쓰는 중)
        for child in out.iterdir():
            if child.name == "logs":
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
    for d in ("T2/factors", "R1/root/factors", "R1/root/events", "R1/work", "R1/events", "R1/overrides", "R3/master", "R3/events", "R3/factors_aux", "R2", "overrides", "logs"):
        (out / d).mkdir(parents=True, exist_ok=True)
    ref = Ref(rc2)
    paths = sorted(set(glob.glob(args.overrides_glob))) if args.overrides_glob else []
    paths = [p for p in paths if kind_of(p)]
    for p in paths:
        shutil.copy2(p, out / "overrides" / (task_dir(p) + "__" + Path(p).name))
    ovr, issues = load_all_overrides(paths, ref) if paths else ({k: pd.DataFrame() for k in KINDS}, [])
    alog = ApplyLog()
    info = {"overrides_files": paths, "counts": {k: int(len(v)) for k, v in ovr.items()}, "steps": {}, "rc2_rules": bool(getattr(args, "rc2_rules", False)),
            "out": str(out)}
    log.info("overrides: %s", info["counts"])
    import ledger_build as lb
    cal = lb.Calendar.from_root(rc2)

    # ---- 1) price → T2 base_data / R3 base_data_aux ----
    t2_base_src, aux_src = stage_v21 / "T2" / "base_data.parquet", r3s / "base_data_aux.parquet"
    t2_base_dst, aux_dst = out / "T2" / "base_data.parquet", out / "R3" / "base_data_aux.parquet"
    pinfo = []
    if len(ovr["price"]):
        aux_ids = set(int(i) for i in _base_ids_of(aux_src))
        t2_ids = set(int(i) for i in ref.ids) - aux_ids
        for src, dst, ids, label in ((t2_base_src, t2_base_dst, t2_ids, "T2/base_data.parquet"), (aux_src, aux_dst, aux_ids, "R3/base_data_aux.parquet")):
            r = apply_price_overrides(ovr["price"], src, dst, alog, ids, label)
            pinfo.append(r)
            if not r["rewritten"]:
                _link(src, dst)
        for r in ovr["price"].itertuples(index=False):
            if r.overrideId not in alog.rows:
                alog.set(r.overrideId, caseId=r.caseId, kind="price", action=r.op, status="rejected", reason="id_not_in_base_data", affected_rows=0, target=str(r.x_tid))
        # R-Y2 P1 ①: estimated price windows -> ledger_build marks split rows corroborated with these prices as estimated
        est = [{"tradingItemId": int(r.x_tid), "dateFrom": pd.Timestamp(parse_date(r.dateFrom)).date(), "dateTo": pd.Timestamp(parse_date(r.dateTo)).date(), "overrideId": r.overrideId}
               for r in ovr["price"].itertuples(index=False) if bool(r.x_isEstimate) and alog.rows.get(r.overrideId, {}).get("status") == "applied"]
        (out / "R1").mkdir(parents=True, exist_ok=True)
        pd.DataFrame(est, columns=["tradingItemId", "dateFrom", "dateTo", "overrideId"]).to_csv(out / "R1" / "price_override_estimates.csv", index=False)
    else:
        _link(t2_base_src, t2_base_dst); _link(aux_src, aux_dst)
    info["steps"]["price"] = pinfo
    log.info("price overrides: %s (%.0fs)", pinfo, time.time() - t0)

    # ---- 1b) aux_additions → R3 aux (identity/prices/lifecycle/factors/dividends via xfeed read-only) ----
    if len(ovr["aux"]):
        asof = json.loads((rc2 / "manifest.json").read_text()).get("asOfDate") if (rc2 / "manifest.json").exists() else None
        ainfo = apply_aux_additions(ovr["aux"], r3s, out / "R3", alog, ref, asof, out / "R3" / "work_aux_rc2f", ref.ledger)
        info["steps"]["aux_additions"] = ainfo
        log.info("aux additions: %s (%.0fs)", ainfo, time.time() - t0)

    # ---- 1c) lifecycle extend_prices → base_data 보충(xfeed; lifecycle 단계의 mark 계산 전에) ----
    if len(ovr["lifecycle"]) and (ovr["lifecycle"].treatment.str.strip() == "extend_prices").any():
        lc_pre = pd.concat([pd.read_parquet(r3s / "master" / "lifecycle.parquet"), pd.read_parquet((out / "R3" / "master" / "lifecycle_aux.parquet") if (out / "R3" / "master" / "lifecycle_aux.parquet").exists() and not (out / "R3" / "master" / "lifecycle_aux.parquet").is_symlink() else r3s / "master" / "lifecycle_aux.parquet")], ignore_index=True)
        aux_ids_now = set(int(i) for i in _base_ids_of(aux_dst))
        cal_pre = pd.read_parquet(rc2 / "calendar.parquet"); cal_pre = cal_pre.loc[cal_pre.isTradingDay, "date"].to_numpy(dtype="datetime64[ns]")
        einfo = apply_extend_prices(ovr["lifecycle"], lc_pre, t2_base_dst, aux_dst, t2_base_src, aux_src, aux_ids_now, alog, out / "R3" / "work_extend_rc2f", cal_pre)
        info["steps"]["extend_prices"] = einfo
        log.info("extend_prices: %s (%.0fs)", einfo, time.time() - t0)

    # ---- 1d) value_overrides → T2 base_data / R3 base_data_aux (price·aux·extend 뒤의 파일에) ----
    if len(ovr["value"]):
        lca_now = out / "R3" / "master" / "lifecycle_aux.parquet"
        lc_v = pd.concat([pd.read_parquet(r3s / "master" / "lifecycle.parquet"), pd.read_parquet(lca_now if lca_now.exists() else r3s / "master" / "lifecycle_aux.parquet")],
                         ignore_index=True).drop_duplicates("tradingItemId", keep="last").set_index("tradingItemId")
        cal_v = pd.read_parquet(rc2 / "calendar.parquet"); cal_v = cal_v.loc[cal_v.isTradingDay, "date"].to_numpy(dtype="datetime64[ns]")
        aux_ids_v = set(int(i) for i in _base_ids_of(aux_dst))
        t2_ids_v = set(int(i) for i in ref.ids) - aux_ids_v
        vinfo = [apply_value_overrides(ovr["value"], src, dst, alog, ids, label, lc_v, cal_v)
                 for src, dst, ids, label in ((t2_base_dst, t2_base_dst, t2_ids_v, "T2/base_data.parquet"), (aux_dst, aux_dst, aux_ids_v, "R3/base_data_aux.parquet"))]
        for r in ovr["value"].itertuples(index=False):
            if r.overrideId not in alog.rows:
                alog.set(r.overrideId, caseId=r.caseId, kind="value", action=f"set_{str(r.field).strip()}", status="rejected", reason="id_not_in_base_data", affected_rows=0, target=str(r.x_tid))
        info["steps"]["value"] = vinfo
        log.info("value overrides: %s (%.0fs)", vinfo, time.time() - t0)

    # ---- 2) split → T2 factors / R3 factors_aux ----
    t2_split_src, aux_split_src = stage_v21 / "T2" / "factors" / "split.parquet", r3s / "factors_aux" / "split.parquet"
    t2_split_dst, aux_split_dst = out / "T2" / "factors" / "split.parquet", out / "R3" / "factors_aux" / "split.parquet"
    if aux_split_dst.exists() and not aux_split_dst.is_symlink():
        aux_split_src = aux_split_dst                     # aux_additions already materialized the aux factors
    _link(stage_v21 / "T2" / "factors" / "dividend.parquet", out / "T2" / "factors" / "dividend.parquet")
    if not (out / "R3" / "factors_aux" / "dividend.parquet").exists():
        _link(r3s / "factors_aux" / "dividend.parquet", out / "R3" / "factors_aux" / "dividend.parquet")
    sinfo = {}
    if len(ovr["split"]):
        t2_split, aux_split = pd.read_parquet(t2_split_src), pd.read_parquet(aux_split_src)
        split_closes = lb.RawCloses([t2_base_dst, aux_dst], set(ovr["split"].x_tid.dropna().astype(int)))     # R-X0 2nd review P1-7: raw-jump check (prices AFTER the price step)
        px_by_tid: dict = {}
        for r in ovr["price"].itertuples(index=False) if len(ovr["price"]) else []:
            if alog.rows.get(r.overrideId, {}).get("status") == "applied":
                px_by_tid.setdefault(int(r.x_tid), []).append(r.overrideId)
        res = apply_split_overrides(ovr["split"], {"T2": (t2_split, set(t2_split.tradingItemId.astype(int))), "aux": (aux_split, set(aux_split.tradingItemId.astype(int)))}, alog, cal, split_closes, px_by_tid)
        # L1 최종: move_date 로 옮긴 경계 목록 → ledger_build 가 eventId 로 지정된 ledger set/drop 대상을 새 날짜로 재매핑(--split-moves)
        lg_split = alog.frame()
        moves = []
        for r in ovr["split"].itertuples(index=False):
            st = lg_split.set_index("overrideId").status.get(r.overrideId, "") if len(lg_split) else ""
            if r.action.strip() == "move_date" and str(st).startswith("applied") and str(r.newExDate).strip():
                moves.append({"tradingItemId": int(r.x_tid), "exDate": pd.Timestamp(parse_date(r.exDate)).date(), "newExDate": pd.Timestamp(parse_date(r.newExDate)).date(), "overrideId": r.overrideId})
        (out / "R1").mkdir(parents=True, exist_ok=True)
        pd.DataFrame(moves, columns=["tradingItemId", "exDate", "newExDate", "overrideId"]).to_csv(out / "R1" / "split_boundary_moves.csv", index=False)
        for label, (df_new, changed), src, dst in (("T2", res["T2"], t2_split_src, t2_split_dst), ("aux", res["aux"], aux_split_src, aux_split_dst)):
            if changed:
                tmp_dst = dst.with_name(dst.name + ".new")
                _write_parquet_like(df_new, src, tmp_dst)
                if dst.is_symlink() or dst.exists():
                    dst.unlink()
                os.replace(tmp_dst, dst)
            elif src != dst:
                _link(src, dst)
            sinfo[label] = {"changed": changed, "rows": int(len(df_new))}
    else:
        _link(t2_split_src, t2_split_dst)
        if aux_split_src != aux_split_dst:
            _link(aux_split_src, aux_split_dst)
    info["steps"]["split"] = sinfo
    # R1 root: rc1_copy 심볼릭 링크 + 교정 factors/split(T2 factors == rc1_copy factors, sha 동일) + events(아래 lifecycle 단계에서 교체 가능)
    for e in ("calendar.parquet", "manifest.json", "README.md", "distributions", "dividends", "master", "qa", "universe"):
        _link(r1s / "rc1_copy" / e, out / "R1" / "root" / e)
    _link(t2_split_dst, out / "R1" / "root" / "factors" / "split.parquet")
    _link(r1s / "rc1_copy" / "factors" / "dividend.parquet", out / "R1" / "root" / "factors" / "dividend.parquet")

    # ---- 3) lineage → R3/master/lineage.parquet ; lifecycle → lifecycle/lifecycle_dates/events/lineage ----
    lc_src, dates_src, ln_src = r3s / "master" / "lifecycle.parquet", r3s / "master" / "lifecycle_dates.parquet", r3s / "master" / "lineage.parquet"
    lca_built = out / "R3" / "master" / "lifecycle_aux.parquet"
    lca_src = lca_built if (lca_built.exists() and not lca_built.is_symlink()) else r3s / "master" / "lifecycle_aux.parquet"
    ev_reclass_src, ev_rc1_src = r3s / "events" / "corporate_actions_reclass.parquet", r1s / "rc1_copy" / "events" / "corporate_actions.parquet"
    ln = pd.read_parquet(ln_src)
    ln_changed = False
    cal_days = pd.read_parquet(rc2 / "calendar.parquet")
    cal_days = cal_days.loc[cal_days.isTradingDay, "date"].to_numpy().astype("datetime64[ns]")
    lc_all = pd.concat([pd.read_parquet(lc_src), pd.read_parquet(lca_src)], ignore_index=True).set_index("tradingItemId")
    if len(ovr["lineage"]):
        ln = apply_lineage_overrides(ovr["lineage"], ln, lc_all, cal_days, alog)
        ln_changed = True
    changed = set()
    if len(ovr["lifecycle"]):
        ids = set(int(i) for i in ovr["lifecycle"].x_tid) | set(int(x) for x in ovr["lifecycle"].successorTradingItemId.map(parse_int) if x is not None and pd.notna(x))
        closes = lb.RawCloses([t2_base_dst, aux_dst], ids)
        top500 = set(pd.read_parquet(rc2 / "master" / "lifecycle_dates.parquet").query("everTop500").tradingItemId.astype(int))
        touched = set()
        if len(ovr["lineage"]):
            touched = {(int(float(a)), int(float(b))) for a, b in zip(ovr["lineage"].oldId, ovr["lineage"].newId) if str(a).strip() and str(b).strip()}
        res = apply_lifecycle_overrides(ovr["lifecycle"], pd.read_parquet(lc_src).set_index("tradingItemId"), pd.read_parquet(lca_src).set_index("tradingItemId"),
                                        pd.read_parquet(dates_src).set_index("tradingItemId"), pd.read_parquet(ev_reclass_src), pd.read_parquet(ev_rc1_src), ln, closes, cal, alog, top500,
                                        cal_days=cal_days, lineage_touched=touched)
        changed = res["changed"]
        if "lifecycle" in changed:
            _write_parquet_like(res["lifecycle"], lc_src, out / "R3" / "master" / "lifecycle.parquet")
        if "lifecycle_aux" in changed:
            _write_parquet_like(res["lifecycle_aux"], lca_src, out / "R3" / "master" / "lifecycle_aux.parquet")
        if "lifecycle_dates" in changed:
            _write_parquet_like(res["lifecycle_dates"], dates_src, out / "R3" / "master" / "lifecycle_dates.parquet")
        if "events_reclass" in changed:
            _write_parquet_like(res["events_reclass"], ev_reclass_src, out / "R3" / "events" / "corporate_actions_reclass.parquet")
            _write_parquet_like(res["events_rc1"], ev_rc1_src, out / "R1" / "root" / "events" / "corporate_actions.parquet")
        if "lineage" in changed:
            ln = res["lineage"]; ln_changed = True
    for name, src in (("lifecycle.parquet", lc_src), ("lifecycle_aux.parquet", lca_src), ("lifecycle_dates.parquet", dates_src), ("aux_instruments.parquet", r3s / "master" / "aux_instruments.parquet")):
        if not (out / "R3" / "master" / name).exists():
            _link(src, out / "R3" / "master" / name)
    if "lifecycle_aux" in changed and lca_src == lca_built:
        _write_parquet_like(res["lifecycle_aux"], r3s / "master" / "lifecycle_aux.parquet", lca_built)
    if ln_changed:
        _write_parquet_like(ln, ln_src, out / "R3" / "master" / "lineage.parquet", extra={"linkVerified": pa.bool_()})     # rc2f-E: keep the new column
    else:
        _link(ln_src, out / "R3" / "master" / "lineage.parquet")
    if not (out / "R3" / "events" / "corporate_actions_reclass.parquet").exists():
        _link(ev_reclass_src, out / "R3" / "events" / "corporate_actions_reclass.parquet")
        _link(r1s / "rc1_copy" / "events" / "corporate_actions.parquet", out / "R1" / "root" / "events" / "corporate_actions.parquet")
    _link(ev_reclass_src.with_name("corporate_actions_reclass.reclass_log.csv"), out / "R3" / "events" / "corporate_actions_reclass.reclass_log.csv")
    for e in ("for_R1_events.csv", "for_R1_aux_dividends.csv", "qa", "work", "events_reclass_input.csv", "price_issue_classification.csv"):
        if (r3s / e).exists() and not (out / "R3" / e).exists():
            _link(r3s / e, out / "R3" / e)
    for t in ("V1", "V2"):
        if (stage_rc2 / t / "qa").exists():
            _link(stage_rc2 / t / "qa", out / t / "qa")
    info["steps"]["lineage_lifecycle"] = {"lineage_changed": ln_changed, "lifecycle_changed": sorted(changed)}
    log.info("lineage/lifecycle overrides applied (%.0fs)", time.time() - t0)

    # ---- 4) ledger_build --overrides ----
    for f in ("work_ciqsplit", "work_ciqsplitcache", "work_divcache_noncash", "work_ciqcurrency"):
        _link(r1s / "work" / f"{f}.parquet", out / "R1" / "work" / f"{f}.parquet")
    shutil.copy2(r1s / "work" / "gap_candidates_union.csv", out / "R1" / "work" / "gap_candidates_union.csv")
    ledger_csvs = [p for p in paths if kind_of(p) == "ledger"]
    for p in ledger_csvs:
        shutil.copy2(p, out / "R1" / "overrides" / (task_dir(p) + "__" + Path(p).name))
    cmd = [PY, str(HERE / "ledger_build.py"), "--root", str(out / "R1" / "root"), "--stage", str(out / "R1"), "--base-data", str(t2_base_dst), "--no-db",
           "--lineage", str(out / "R3" / "master" / "lineage.parquet"), "--lifecycle", str(out / "R3" / "master" / "lifecycle.parquet"),
           "--aux-base-data", str(aux_dst), "--r3-events", str(out / "R3" / "for_R1_events.csv"),
           "--gap-candidates", str(stage_rc2 / "R2" / "qa" / "returns_v2" / "ledger_gap_candidates.csv")]
    if ledger_csvs:
        cmd += ["--overrides"] + sorted(str(p) for p in (out / "R1" / "overrides").glob("*.csv")) + ["--overrides-ref-ledger", str(rc2 / "events" / "ca_ledger.parquet")]
        if (out / "R1" / "split_boundary_moves.csv").exists():
            cmd += ["--split-moves", str(out / "R1" / "split_boundary_moves.csv")]
    if (out / "R1" / "price_override_estimates.csv").exists():
        cmd += ["--estimated-prices", str(out / "R1" / "price_override_estimates.csv")]
    if args.rc2_rules:
        cmd += ["--rc2-rules"]
    (out / "logs").mkdir(exist_ok=True)
    if args.no_ledger:
        info["steps"]["ledger"] = "skipped(--no-ledger)"
        for r in ovr["ledger"].itertuples(index=False) if len(ovr["ledger"]) else []:
            alog.set(r.overrideId, caseId=r.caseId, kind="ledger", action=r.action, status="skipped", reason="--no-ledger", affected_rows=0, target=r.eventId or "")
    else:
        log.info("ledger_build: %s", " ".join(cmd))
        with open(out / "logs" / "ledger_build.log", "w") as fh:
            rc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, cwd=str(HERE)).returncode
        if rc != 0:
            raise SystemExit(f"ledger_build failed (exit {rc}); see {out / 'logs' / 'ledger_build.log'}")
        binfo = json.loads((out / "R1" / "events" / "ca_ledger_build_info.json").read_text())
        info["steps"]["ledger"] = {"rows": binfo["file"]["rows"], "sha256": binfo["file"]["sha256"], "csv_overrides": binfo.get("csv_overrides")}
        res_csv = out / "R1" / "events" / "ca_ledger_csv_overrides.csv"
        if res_csv.exists():
            for r in pd.read_csv(res_csv, dtype=str, keep_default_na=False).itertuples(index=False):
                alog.set(r.overrideId, caseId=r.caseId, kind="ledger", action=r.action, status=r.status, reason=r.reason, affected_rows=int(float(r.affected_rows or 0)),
                         target=r.target, file="ledger", eventIds=r.eventIds)
        for r in ovr["ledger"].itertuples(index=False) if len(ovr["ledger"]) else []:
            if r.overrideId not in alog.rows:
                alog.set(r.overrideId, caseId=r.caseId, kind="ledger", action=r.action, status="rejected", reason="not_reported_by_ledger_build", affected_rows=0)
        # ---- 4b) R-X4 (d): settlementReturnScope labels + legs after the settlement bar (warning list) ----
        if not args.rc2_rules:
            led_new = pd.read_parquet(out / "R1" / "events" / "ca_ledger.parquet")
            lc_all2 = pd.concat([pd.read_parquet(out / "R3" / "master" / "lifecycle.parquet"), pd.read_parquet(out / "R3" / "master" / "lifecycle_aux.parquet")], ignore_index=True)
            ev_p = out / "R3" / "events" / "corporate_actions_reclass.parquet"
            ev_df2 = pd.read_parquet(ev_p if not ev_p.is_symlink() else ev_reclass_src)
            ev_df2, warn_legs, n_scope = label_settlement_scope(ev_df2, led_new, lc_all2, cal, alog)
            if n_scope:
                if ev_p.is_symlink():
                    ev_p.unlink()
                _write_parquet_like(_events_types(ev_df2), ev_reclass_src, ev_p)
            warn_legs.to_csv(out / "R1" / "events" / "ca_ledger_legs_after_settlement_bar.csv", index=False)
            info["steps"]["settlement_scope"] = {"events_rows_relabelled": int(n_scope), "value_legs_after_settlement_bar": int(len(warn_legs))}
            log.info("settlementReturnScope (R-X4 d): %d events rows -> events_legs_only; %d value legs after the settlement bar (warning list)", n_scope, len(warn_legs))
    # ---- 5) log ----
    lg = alog.frame()
    lg.to_csv(out / "rc2f_apply_log.csv", index=False)
    all_ids = set()
    for k, v in ovr.items():
        if len(v):
            all_ids |= set(v.overrideId)
    missing = sorted(all_ids - set(lg.overrideId))
    info["apply_log"] = {"rows": int(len(lg)), "status": lg.status.value_counts().to_dict() if len(lg) else {}, "override_ids_missing_from_log": missing}
    info["seconds"] = round(time.time() - t0, 1)
    info["at"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    (out / "rc2f_apply_info.json").write_text(json.dumps(info, indent=1, default=str))
    print(json.dumps(info, indent=1, default=str))
    if missing:
        raise SystemExit(f"GATE FAIL: override ids not in apply log: {missing}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate", help="교정 CSV 스키마 검증(오류면 exit 1)")
    v.add_argument("csv", nargs="+", help="교정 CSV(글롭 가능). 종류는 파일명 접두어 split_/price_/ledger_/lifecycle_/lineage_")
    v.add_argument("--rc2", type=Path, default=RC2_DEFAULT, help="참조 rc2 빌드(읽기 전용)")
    v.add_argument("--report", type=Path, default=None, help="이슈 목록 csv 저장")
    v.add_argument("--conflicts", type=Path, default=None, help="같은 대상을 두 교정이 건드리는 목록(교차 태스크 포함) csv 저장")
    v.add_argument("--width", type=int, default=160)
    a = sub.add_parser("apply", help="교정 적용 + ledger_build (출력: publish.py --rc2-stage 레이아웃)")
    a.add_argument("--rc2", type=Path, default=RC2_DEFAULT)
    a.add_argument("--stage-rc2", type=Path, default=STAGE_RC2_DEFAULT)
    a.add_argument("--stage-v21", type=Path, default=STAGE_V21_DEFAULT)
    a.add_argument("--overrides-glob", default=str(Path(__file__).resolve().parent / "overrides" / "rc2f" / "X*" / "*.csv"))
    a.add_argument("--out", type=Path, required=True)
    a.add_argument("--force", action="store_true", help="기존 --out 삭제 후 재생성")
    a.add_argument("--no-ledger", action="store_true", help="ledger_build 생략(교정 파일만)")
    a.add_argument("--rc2-rules", action="store_true", help="rc2 규칙 그대로(ledger_build --rc2-rules, scope 라벨 생략) — 비트 동일 게이트용")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.cmd == "validate":
        return cmd_validate(args)
    if args.cmd == "apply":
        return cmd_apply(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
