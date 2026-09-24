#!/home/jgo/usr/venv-3.11/bin/python
"""cache_v21/rc2f_recount.py - rc2f X0: 재생성 결과 재집계 + 귀속 게이트.

입력: rc2 발행 빌드(--rc2), rc2f 재생성 빌드(--build = rc2f_build.sh 의 publish 출력), rc2f 스테이지(--stage: rc2f_apply_log.csv,
R1/events/ca_ledger_csv_overrides.csv, overrides/*.csv), 레지스트리(--registry: registry_cases_v0.csv, flag4_rows_with_cause.csv).

출력(--out):
  returns_diff.parquet     rc2 대비 바뀐 returns 셀 전부(|Δret|>1e-9, retFlag 변화, 행 추가/삭제) + 귀속(attributedTo overrideId, how)
  ledger_diff.csv          rc2 대비 바뀐 원장 행(키 (id, exDate, eventType, legType, child): added/removed/changed 컬럼) + 귀속
  registry_after.csv       registry_cases_v0 + 사건마다 재생성 후 상태(overrideIds/Status, retFlag before/after(max, 창 안), flag4 잔여, 원장 status before/after, 바뀐 셀 수)
  flag4_after.csv          flag4_rows_with_cause 584 행마다 ret/retFlag after, changed, resolved
  summary.json / summary.md 584 → 잔여 n(원인별·목록별), 교정 적용 수(kind×status), 바뀐 셀 수, 귀속 게이트 결과
귀속 게이트: 바뀐 returns 셀 전부가 어떤 overrideId 의 영향 범위(같은 id 의 exDate/유효 구간 + 다음 관측일, lineage 링크 첫 행, stitched 복사 id,
자회사/승계사/부모 id, 원장 변경 행의 파급)에 귀속돼야 함. 귀속 안 되는 셀·원장 행이 있으면 exit 1 (FAIL).
R-X0 P1-5/P2-6: 원장 변경 행의 '같은 id' 귀속은 전 기간이 아니라 교정 사건 창(사건일 −LEDGER_ROW_TOL_DAYS .. 다음 관측일 +LEDGER_ROW_TOL_DAYS;
lifecycle 은 컷 이후 꼬리, lineage 는 효력일 ±LINEAGE_ROW_TOL_DAYS, aux 는 전 기간) 안의 exDate 만 잡고, 셀·행 모두 일치하는 귀속을 전부 기록한다
(attributedTo 는 ';' 결합, attributedPrimary 는 우선순위 첫 항목: 직접 창 > 원장 행 창 > 규칙 창 / repairOf 태그 > keeper 태그 > 같은 id 창 > 규칙).
규칙: RULE-X3-1/3/4, RULE-X4-align(정산 bar 정렬 leg), RULE-X5-link (RULE-X3-5 정산 현금 규칙은 R-X0 P0 로 제거).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

SP = Path(os.environ.get("RC2F_SP", "/tmp/claude-1008/-home-jgo/3f2abe1d-a2a7-4017-9ac1-2c7cb961e311/scratchpad"))   # R-X0 P2-5: env override
DAY_BITS, DAY_OFFSET = 20, 30000
RET_TOL = 1e-9
LEDGER_KEY = ["tradingItemId", "exDate", "eventType", "legType", "childTradingItemId"]
LEDGER_CMP = ["cashPerShare", "parentFactor", "childRatio", "childValueSource", "childValuePerOldShare", "status", "isEstimate", "source", "payDate"]
LEDGER_ROW_TOL_DAYS = 10       # 원장 행 '같은 id' 귀속: 사건 창 양쪽 여유(정산 bar 정렬 ≤ 7 일, 마커 창 등)
LINEAGE_ROW_TOL_DAYS = 45      # lineage/승계 교정: 효력일·정산일 ± (WI 겹침, reassigned copy 창 30 일)


def _rss_gb() -> float:
    try:
        import resource
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6, 2)
    except Exception:  # noqa: BLE001
        return float("nan")


def _days(x) -> np.ndarray:
    return np.asarray(x).astype("datetime64[D]").astype(np.int64)


def _keys(ids, days) -> np.ndarray:
    return (np.asarray(ids, dtype=np.int64) << DAY_BITS) | (np.asarray(days, dtype=np.int64) + DAY_OFFSET)


def key_ids(k: np.ndarray) -> np.ndarray:
    return np.asarray(k, dtype=np.int64) >> DAY_BITS


def key_days(k: np.ndarray) -> np.ndarray:
    return (np.asarray(k, dtype=np.int64) & ((1 << DAY_BITS) - 1)) - DAY_OFFSET


def load_returns(path: Path) -> dict:
    """정렬된 key(id<<20|day) + ret + flag 만 보유(48M 행 ≈ 0.8 GB); id/day 는 key 에서 복원."""
    t = pq.read_table(path, columns=["date", "tradingItemId", "ret", "retFlag"])
    k = _keys(t.column("tradingItemId").to_numpy().astype(np.int64), _days(t.column("date").to_numpy()))
    order = np.argsort(k, kind="stable")
    out = {"key": k[order], "ret": t.column("ret").to_numpy(zero_copy_only=False)[order], "flag": t.column("retFlag").to_numpy(zero_copy_only=False).astype(np.int8)[order]}
    del t, k, order
    return out


def returns_diff(old: dict, new: dict) -> pd.DataFrame:
    """바뀐 셀: value(|Δ|>1e-9 또는 NaN 여부 변화), flag, added(new 만), removed(old 만)."""
    common, io, inew = np.intersect1d(old["key"], new["key"], assume_unique=True, return_indices=True)
    ro, rn = old["ret"][io], new["ret"][inew]
    fo, fn = old["flag"][io], new["flag"][inew]
    both_nan = np.isnan(ro) & np.isnan(rn)
    val = ~both_nan & (np.isnan(ro) != np.isnan(rn) | (np.abs(ro - rn) > RET_TOL))
    flg = fo != fn
    parts = []
    m = val | flg
    if m.any():
        kk = common[m]
        parts.append(pd.DataFrame({"tradingItemId": key_ids(kk), "day": key_days(kk), "retOld": ro[m], "retNew": rn[m], "flagOld": fo[m], "flagNew": fn[m],
                                   "kind": np.where(val[m] & flg[m], "value+flag", np.where(val[m], "value", "flag"))}))
    only_old = np.setdiff1d(np.arange(len(old["key"])), io, assume_unique=True)
    if len(only_old):
        kk = old["key"][only_old]
        parts.append(pd.DataFrame({"tradingItemId": key_ids(kk), "day": key_days(kk), "retOld": old["ret"][only_old], "retNew": np.nan,
                                   "flagOld": old["flag"][only_old], "flagNew": -1, "kind": "removed"}))
    only_new = np.setdiff1d(np.arange(len(new["key"])), inew, assume_unique=True)
    if len(only_new):
        kk = new["key"][only_new]
        parts.append(pd.DataFrame({"tradingItemId": key_ids(kk), "day": key_days(kk), "retOld": np.nan, "retNew": new["ret"][only_new],
                                   "flagOld": -1, "flagNew": new["flag"][only_new], "kind": "added"}))
    if not parts:
        return pd.DataFrame(columns=["tradingItemId", "day", "retOld", "retNew", "flagOld", "flagNew", "kind", "date"])
    d = pd.concat(parts, ignore_index=True)
    d["date"] = d.day.astype("datetime64[D]").astype("datetime64[ns]")
    return d.sort_values(["tradingItemId", "day"]).reset_index(drop=True)


class ObsDates:
    """id 별 관측일(새 빌드 ∪ rc2) — 사건일 이후 첫 관측일 계산."""

    def __init__(self, *rets):
        k = np.unique(np.concatenate([r["key"] for r in rets]))
        self.ids = key_ids(k)
        self.days = key_days(k)
        self.starts = {}
        u, first = np.unique(self.ids, return_index=True)
        ends = np.r_[first[1:], len(self.ids)]
        self.starts = {int(i): (int(a), int(b)) for i, a, b in zip(u, first, ends)}

    def first_on_or_after(self, tid: int, day: int) -> int | None:
        s = self.starts.get(int(tid))
        if s is None:
            return None
        a, b = s
        pos = a + np.searchsorted(self.days[a:b], day, side="left")
        return int(self.days[pos]) if pos < b else None

    def first_day(self, tid: int) -> int | None:
        s = self.starts.get(int(tid))
        return int(self.days[s[0]]) if s else None

    def last_day(self, tid: int) -> int | None:
        s = self.starts.get(int(tid))
        return int(self.days[s[1] - 1]) if s else None


def ledger_diff(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """키 (id, exDate, eventType, legType, child) 기준 added/removed/changed(어느 컬럼)."""
    def prep(df):
        d = df.copy()
        d["childTradingItemId"] = d.childTradingItemId.astype("Int64").fillna(-1).astype("int64")
        d["exDate"] = pd.to_datetime(d.exDate)
        d["payDate"] = pd.to_datetime(d.payDate)
        d = d.sort_values(LEDGER_KEY + ["eventId"]).drop_duplicates(LEDGER_KEY, keep="first")
        return d.set_index(LEDGER_KEY)
    o, n = prep(old), prep(new)
    idx_o, idx_n = o.index, n.index
    added = n.loc[idx_n.difference(idx_o)]
    removed = o.loc[idx_o.difference(idx_n)]
    common = idx_o.intersection(idx_n)
    oc, nc = o.loc[common], n.loc[common]
    changed_cols = []
    for c in LEDGER_CMP:
        a, b = oc[c], nc[c]
        if c in ("cashPerShare", "parentFactor", "childRatio", "childValuePerOldShare"):
            a, b = a.astype(float), b.astype(float)
            diff = ~((a == b) | (a.isna() & b.isna()) | (np.abs(a - b) <= 1e-12))
        elif c == "payDate":
            diff = ~((a == b) | (a.isna() & b.isna()))
        else:
            diff = a.astype(str) != b.astype(str)
        changed_cols.append(diff.rename(c))
    cc = pd.concat(changed_cols, axis=1)
    chg_mask = cc.any(axis=1)
    rows = []
    for idx, r in added.iterrows():
        rows.append(dict(zip(LEDGER_KEY, idx), change="added", columns="", eventIdOld="", eventIdNew=r.eventId, repairOfNew=r.repairOf or "", sourceNew=r.source, sourceOld="",
                         keeperNew=r.keeperEventId or "", cashNew=r.cashPerShare, ratioNew=r.childRatio, statusOld="", statusNew=r.status, notesNew=r.notes or ""))
    for idx, r in removed.iterrows():
        rows.append(dict(zip(LEDGER_KEY, idx), change="removed", columns="", eventIdOld=r.eventId, eventIdNew="", repairOfNew="", sourceNew="", sourceOld=r.source,
                         keeperNew=r.keeperEventId or "", cashNew=r.cashPerShare, ratioNew=r.childRatio, statusOld=r.status, statusNew="", notesNew=""))
    for idx in common[chg_mask.to_numpy()]:
        cols = [c for c in LEDGER_CMP if bool(cc.loc[idx, c])]
        r = nc.loc[idx]
        rows.append(dict(zip(LEDGER_KEY, idx), change="changed", columns=",".join(cols), eventIdOld=oc.loc[idx].eventId, eventIdNew=r.eventId,
                         repairOfNew=r.repairOf or "", sourceNew=r.source, sourceOld=oc.loc[idx].source, keeperNew=r.keeperEventId or "", cashNew=r.cashPerShare, ratioNew=r.childRatio,
                         statusOld=oc.loc[idx].status, statusNew=r.status, notesNew=r.notes or ""))
    out = pd.DataFrame(rows, columns=LEDGER_KEY + ["change", "columns", "eventIdOld", "eventIdNew", "repairOfNew", "sourceNew", "sourceOld", "keeperNew", "cashNew", "ratioNew", "statusOld", "statusNew", "notesNew"])
    return out.sort_values(LEDGER_KEY).reset_index(drop=True)


# --------------------------------------------------------------------------------------
# 귀속
# --------------------------------------------------------------------------------------
class Scope:
    """overrideId 별 영향 범위: id 집합 + (id, dayFrom, dayTo) 창. cell(id, day) → overrideId 목록."""

    def __init__(self):
        self.windows: list[tuple[int, int, int, str, str]] = []     # (tid, from, to, oid, how): returns 셀 창
        self.id_windows: list[tuple[int, int, int, str, str]] = []  # (tid, from, to, oid, how): 원장 행(exDate) 창 — 같은 id 파급(R-X0 P1-5: 사건 창으로 제한)
        self.ids: dict[int, set] = {}                                # tid -> {oid} (요약용)

    def add_window(self, tid, d0, d1, oid, how):
        if tid is None or d0 is None:
            return
        self.windows.append((int(tid), int(d0), int(d1) if d1 is not None else 1 << 40, oid, how))

    def add_id(self, tid, oid, how, d0=None, d1=None):
        """원장 행 귀속 창: exDate ∈ [d0, d1] (None 은 열린 끝; 둘 다 None 이면 전 기간 — aux 신규 id 에만)."""
        if tid is None:
            return
        self.ids.setdefault(int(tid), set()).add(oid)
        self.id_windows.append((int(tid), int(d0) if d0 is not None else -(1 << 40), int(d1) if d1 is not None else 1 << 40, oid, how))

    @staticmethod
    def _index(rows):
        if rows:
            w = pd.DataFrame(rows, columns=["tid", "d0", "d1", "oid", "how"])
            return {t: g[["d0", "d1", "oid", "how"]].to_numpy() for t, g in w.groupby("tid")}
        return {}

    def build(self):
        self.by_tid = self._index(self.windows)
        self.ids_by_tid = self._index(self.id_windows)

    @staticmethod
    def _hits(g, day):
        if g is None:
            return []
        hit = (g[:, 0].astype(np.int64) <= day) & (day <= g[:, 1].astype(np.int64))
        return [(o, h) for o, h in zip(g[hit, 2], g[hit, 3])]

    def lookup(self, tid: int, day: int) -> list[tuple[str, str]]:
        return self._hits(self.by_tid.get(int(tid)), day)

    def lookup_id(self, tid: int, day: int) -> list[tuple[str, str]]:
        return self._hits(self.ids_by_tid.get(int(tid)), day)


def build_scope(alog: pd.DataFrame, overrides: dict, obs: ObsDates, ledger_old: pd.DataFrame, ledger_new: pd.DataFrame, lineage_old: pd.DataFrame,
                lineage_new: pd.DataFrame, lifecycle_old: pd.DataFrame, lifecycle_new: pd.DataFrame, stitched: pd.DataFrame | None) -> Scope:
    sc = Scope()
    links = {}
    for ln in (lineage_old, lineage_new):
        for r in ln[ln.relation.isin(["rename", "relist"])].itertuples(index=False):
            links.setdefault(int(r.oldId), set()).add(int(r.newId))
    copies = {}
    for L in (ledger_old, ledger_new):
        c = L[L.source == "stitched_copy"]
        for k, t in zip(c.keeperEventId, c.tradingItemId):
            copies.setdefault(str(k), set()).add(int(t))
    keeper_tid = {}
    for L in (ledger_old, ledger_new):
        for e, t in zip(L.eventId, L.tradingItemId):
            keeper_tid[str(e)] = int(t)
    if stitched is not None and len(stitched):
        for r in stitched.itertuples(index=False):
            k = str(getattr(r, "keeperEventId", "") or "")
            if k:
                copies.setdefault(k, set()).add(int(r.tradingItemId))

    def event_window(tid, day, oid, how):
        """사건일 → 그 id 의 사건일 이후 첫 관측일까지(+ 그 다음 관측일: 사건이 첫 관측 행이면 그 다음 행의 prev 가 바뀜).
        원장 행 창은 [day − TOL, 끝 + TOL] (R-X0 P1-5)."""
        d1 = obs.first_on_or_after(tid, day)
        if d1 is None:
            end = day
        else:
            d2 = obs.first_on_or_after(tid, d1 + 1)
            end = d2 if d2 is not None else d1
        sc.add_window(tid, day, end, oid, how)
        sc.add_id(tid, oid, how, day - LEDGER_ROW_TOL_DAYS, end + LEDGER_ROW_TOL_DAYS)
        for succ in links.get(int(tid), ()):          # lineage 링크 새 id 첫 행(옛 id 갭 사건 적용)
            f = obs.first_day(succ)
            if f is not None:
                sc.add_window(succ, f, f, oid, how + "->linked_first_row")
                sc.add_id(succ, oid, how + "->linked", min(f, day) - LEDGER_ROW_TOL_DAYS, max(f, end) + LEDGER_ROW_TOL_DAYS)

    def copies_of(event_id, oid, how, day):
        for t in copies.get(str(event_id), ()):
            event_window(t, day, oid, how + "->stitched_copy")

    lc_old = lifecycle_old.set_index("tradingItemId").lastTradeDate if lifecycle_old is not None else pd.Series(dtype="datetime64[ns]")
    lc_new = lifecycle_new.set_index("tradingItemId").lastTradeDate if lifecycle_new is not None else pd.Series(dtype="datetime64[ns]")
    for r in alog.itertuples(index=False):
        oid, kind = r.overrideId, r.kind
        if r.status not in ("applied", "applied_no_effect", "applied_unverified", "applied_unchecked"):      # applied_no_effect (X5 set_ratio with chainGap > 5): the ledger rename/relist row still changes (R-X0 2nd review P1-6); applied_unverified (split raw-jump check failed) is applied
            continue
        ov = overrides.get(kind)
        row = ov[ov.overrideId == oid].iloc[0] if ov is not None and (ov.overrideId == oid).any() else None
        if kind == "price" and row is not None:
            tid = int(row.tradingItemId); d0 = _days(np.array([pd.Timestamp(row.dateFrom)], dtype="datetime64[ns]"))[0]; d1 = _days(np.array([pd.Timestamp(row.dateTo)], dtype="datetime64[ns]"))[0]
            nxt = obs.first_on_or_after(tid, d1 + 1)
            est_px = str(getattr(row, "isEstimate", "") or "").strip().lower() in ("true", "1", "yes")
            sc.add_window(tid, d0, nxt if nxt is not None else d1, oid, "price_override(estimate)" if est_px else "price_window(+next obs)")
            sc.add_id(tid, oid, "price_id", d0 - LEDGER_ROW_TOL_DAYS, (nxt if nxt is not None else d1) + LEDGER_ROW_TOL_DAYS)
            for L in (ledger_old, ledger_new):        # 이 id 가 자회사인 사건(자회사 종가 평가) → 부모 셀
                ch = L[(L.childTradingItemId.astype("Int64") == tid)]
                for p, ex in zip(ch.tradingItemId, ch.exDate):
                    exd = int(_days(np.array([ex], dtype="datetime64[ns]"))[0])
                    if d0 - 10 <= exd <= d1 + 10:
                        event_window(int(p), exd, oid, "price->child_valuation_parent")
            for succ in links.get(tid, ()):
                f = obs.first_day(succ)
                if f is not None:
                    sc.add_window(succ, f, f, oid, "price->linked_first_row")
        elif kind == "split" and row is not None:
            tid = int(row.tradingItemId)
            for dcol in ("exDate", "newExDate"):
                v = str(getattr(row, dcol, "") or "").strip()
                if v:
                    event_window(tid, int(_days(np.array([pd.Timestamp(v)], dtype="datetime64[ns]"))[0]), oid, f"split_{row.action}({dcol})")
        elif kind == "ledger":
            tgt = str(r.target or "")
            try:
                tid = int(tgt.split("@")[0]); day = int(_days(np.array([pd.Timestamp(tgt.split("@")[1].split("/")[0])], dtype="datetime64[ns]"))[0])
            except (ValueError, IndexError):
                tid, day = None, None
            if tid is not None:
                event_window(tid, day, oid, f"ledger_{r.action}(target)")
            if row is not None and str(row.set).strip():
                try:
                    sd = json.loads(row.set)
                    if sd.get("exDate"):
                        event_window(tid if tid is not None else int(row.tradingItemId), int(_days(np.array([pd.Timestamp(sd["exDate"])], dtype="datetime64[ns]"))[0]), oid, "ledger_set(exDate)")
                except (json.JSONDecodeError, ValueError):
                    pass
            for eid in str(r.eventIds or "").split(";"):
                if eid:
                    for L in (ledger_new,):
                        m = L[L.eventId == eid]
                        for t, ex in zip(m.tradingItemId, m.exDate):
                            event_window(int(t), int(_days(np.array([ex], dtype="datetime64[ns]"))[0]), oid, "ledger(eventId)")
                    copies_of(eid, oid, "ledger", day if day is not None else 0)
            if tid is not None:
                for L in (ledger_old,):
                    m = L[(L.tradingItemId == tid) & (L.exDate == pd.Timestamp(np.int64(day).astype("datetime64[D]")))]
                    for eid in m.eventId:
                        copies_of(eid, oid, "ledger(old row)", day)
        elif kind == "lifecycle" and row is not None:
            tid = int(row.tradingItemId)
            lo = pd.to_datetime(lc_old.get(tid, pd.NaT)); ln_ = pd.to_datetime(lc_new.get(tid, pd.NaT))
            cut = min([x for x in (lo, ln_) if pd.notna(x)], default=pd.NaT)
            days_ = [int(_days(np.array([pd.Timestamp(v)], dtype="datetime64[ns]"))[0]) for dcol in ("settlementDate", "payDate", "effectiveDate", "lastTradeDate")
                     for v in [str(getattr(row, dcol, "") or "").strip()] if v]
            if pd.notna(cut):
                cut_d = int(_days(np.array([cut], dtype="datetime64[ns]"))[0])
                sc.add_window(tid, cut_d, None, oid, "lifecycle_tail(from min(old,new lastTradeDate))")
                sc.add_id(tid, oid, "lifecycle_tail", min([cut_d] + days_) - LEDGER_ROW_TOL_DAYS, None)      # 꼬리 행(정산 leg·종료 뒤 드롭) 전부
            elif days_:
                sc.add_id(tid, oid, "lifecycle_id", min(days_) - LEDGER_ROW_TOL_DAYS, None)
            else:
                sc.add_id(tid, oid, "lifecycle_id")
            for dcol in ("settlementDate", "payDate", "effectiveDate", "lastTradeDate"):
                v = str(getattr(row, dcol, "") or "").strip()
                if v:
                    event_window(tid, int(_days(np.array([pd.Timestamp(v)], dtype="datetime64[ns]"))[0]), oid, f"lifecycle({dcol})")
            s = str(row.successorTradingItemId or "").strip()
            if s:
                succ = int(float(s)); f = obs.first_day(succ)
                anchor = days_ + ([int(_days(np.array([cut], dtype="datetime64[ns]"))[0])] if pd.notna(cut) else [])
                if anchor:      # 승계사 행: 전환 창(정산일 ± LINEAGE_ROW_TOL_DAYS: 옛 id 행 이동·복사)
                    sc.add_id(succ, oid, "lifecycle_successor", min(anchor) - LINEAGE_ROW_TOL_DAYS, max(anchor) + LINEAGE_ROW_TOL_DAYS)
                else:
                    sc.add_id(succ, oid, "lifecycle_successor")
                for dcol in ("settlementDate", "payDate", "effectiveDate", "lastTradeDate"):
                    v = str(getattr(row, dcol, "") or "").strip()
                    if v:
                        event_window(succ, int(_days(np.array([pd.Timestamp(v)], dtype="datetime64[ns]"))[0]), oid, f"lifecycle_successor({dcol})")
        elif kind == "aux" and row is not None:
            tid = int(row.tradingItemId)
            sc.add_window(tid, 0, None, oid, "aux_addition(all rows of the new aux id)")
            sc.add_id(tid, oid, "aux_id")          # 신규 id: 전 기간
        elif kind == "lineage" and row is not None:
            old, new = int(row.oldId), int(row.newId)
            f = obs.first_day(new)
            if f is not None:
                sc.add_window(new, f, f, oid, "lineage_new_first_row")
            effs = []
            for ln in (lineage_old, lineage_new):
                m = ln[(ln.oldId == old) & (ln.newId == new)]
                for e in m.effectiveDate:
                    if pd.notna(e):
                        effs.append(int(_days(np.array([e], dtype="datetime64[ns]"))[0]))
                        event_window(old, effs[-1], oid, "lineage(effectiveDate)")
            v = str(row.effectiveDate or "").strip()
            if v:
                effs.append(int(_days(np.array([pd.Timestamp(v)], dtype="datetime64[ns]"))[0]))
                event_window(old, effs[-1], oid, "lineage(csv effectiveDate)")
            anchor = effs + ([f] if f is not None else [])
            if anchor:      # 계보 교정: 효력일/새 id 첫 행 ± LINEAGE_ROW_TOL_DAYS 의 옛/새 id 행(lineage 행, 같은 날 split 병합, 전환 귀속)
                sc.add_id(new, oid, "lineage_new", min(anchor) - LINEAGE_ROW_TOL_DAYS, max(anchor) + LINEAGE_ROW_TOL_DAYS)
                sc.add_id(old, oid, "lineage_old", min(anchor) - LINEAGE_ROW_TOL_DAYS, max(anchor) + LINEAGE_ROW_TOL_DAYS)
            else:
                sc.add_id(new, oid, "lineage_new"); sc.add_id(old, oid, "lineage_old")
            if str(getattr(row, "linkVerified", "") or "").strip().lower() in ("true", "1", "yes"):
                # rc2f-E: verified link -> the link row (new id first row, or the first new-id row after the old id's lifecycle-capped last day)
                # changes flag 4 -> regular; attribute those cells with how link_verified
                if f is not None:
                    sc.add_window(new, f, f, oid, "link_verified")
                lo = obs.last_day(old)
                lt = pd.to_datetime(lc_new.get(old, pd.NaT))
                if lo is not None and pd.notna(lt):
                    lo = min(lo, int(_days(np.array([lt], dtype="datetime64[ns]"))[0]))
                if lo is not None and f is not None and lo >= f:
                    d1 = obs.first_on_or_after(new, lo + 1)
                    if d1 is not None:
                        sc.add_window(new, d1, d1, oid, "link_verified")
    sc.build()
    sc.links, sc.copies, sc.keeper_tid = links, copies, keeper_tid
    return sc


def attribute_ledger_rows(ld: pd.DataFrame, sc: Scope, ledger_new: pd.DataFrame, ledger_old: pd.DataFrame, rules: bool = True, lineage: pd.DataFrame | None = None) -> pd.DataFrame:
    """원장 변경 행 → overrideId: repairOf 태그 > stitched keeper 태그 > 같은 id(부모/자회사) 사건 창(exDate ∈ 창, R-X0 P1-5) > (rules) rc2f 규칙 변경:
    RULE-X3-1 reassigned 행의 원래 id stitched_copy 추가, RULE-X3-3 벤더 스텝 마커 창(제거·상태 변경), RULE-X3-4 전환일 귀속(옛 id 행 → 승계 id 행 이동),
    RULE-X5-link lineage 행 status(chainable/ratioSource), RULE-X4-align 정산 bar 로 옮긴 value leg(notes exDate_aligned_to_settlement_bar; 옛 날짜 행 포함).
    split 행 notes 의 src=...|rc2f:<id> 태그(교정으로 스케일된 경계) 도 그 overrideId 로 귀속. 일치하는 귀속은 전부 기록(attributedTo ';' 결합, attributedPrimary = 첫 항목, nAttributions)."""
    pairs = set()
    if lineage is not None and len(lineage):
        pairs = set(zip(lineage.oldId.astype(int), lineage.newId.astype(int))) | set(zip(lineage.newId.astype(int), lineage.oldId.astype(int)))
    # X3-4: removed row on id A + added row on id B (lineage pair) with the same exDate/leg/child/amount
    def amt_key(r):
        return (pd.Timestamp(r.exDate), r.legType, int(r.childTradingItemId), round(float(r.cashNew), 4) if pd.notna(r.cashNew) else None, round(float(r.ratioNew), 4) if pd.notna(r.ratioNew) else None)
    added_by_key = {}
    for r in ld[ld.change == "added"].itertuples(index=False):
        added_by_key.setdefault(amt_key(r), set()).add(int(r.tradingItemId))
    moved_ids = set()
    # the old id's row either disappears or turns into a stitched_copy of the successor's (now real) row
    gone = ld[(ld.change == "removed") | ((ld.change == "changed") & (ld.sourceNew.astype(str) == "stitched_copy") & (ld.sourceOld.astype(str) != "stitched_copy"))]
    for r in gone.itertuples(index=False):
        for b in added_by_key.get(amt_key(r), ()):
            if (int(r.tradingItemId), b) in pairs or not pairs:
                moved_ids.add((int(r.tradingItemId),) + amt_key(r)); moved_ids.add((b,) + amt_key(r))
    keeper_tag = {}
    for L in (ledger_new, ledger_old):
        for e, rep in zip(L.eventId, L.repairOf):
            if isinstance(rep, str) and "csv_override:" in rep:
                keeper_tag[str(e)] = ";".join(t.split(":", 1)[1] for t in rep.split(";") if t.startswith("csv_override:"))
    notes_col = ld.notesNew.astype(str) if "notesNew" in ld.columns else pd.Series("", index=ld.index)
    # RULE-X4-align (R-X4 §4-2): value legs moved to the parent's settlement bar — the moved row carries notes exDate_aligned_to_settlement_bar(from=<old exDate>);
    # the row at the old date disappears (removed) or, when another row shared that key, shows as changed
    aligned_from = set()
    for r, nt in zip(ld.itertuples(index=False), notes_col):
        m = re.search(r"exDate_aligned_to_settlement_bar\(from=(\d{4}-\d{2}-\d{2})", nt)
        if m:
            aligned_from.add((int(r.tradingItemId), r.legType, int(r.childTradingItemId), pd.Timestamp(m.group(1))))
    out_oid, out_how, out_primary, out_n = [], [], [], []
    for r, nt in zip(ld.itertuples(index=False), notes_col):
        cands: list[tuple[str, str]] = []            # (oid, how) in precedence order; all matches are kept
        rep = str(r.repairOfNew or "")
        tags = [t.split(":", 1)[1] for t in rep.split(";") if t.startswith("csv_override:")]
        if tags:
            cands.append((";".join(tags), "repairOf_tag"))
        if r.keeperNew and str(r.keeperNew) in keeper_tag:
            cands.append((keeper_tag[str(r.keeperNew)], "stitched_copy_of_tagged_keeper"))
        for t in re.findall(r"rc2f(?:_est|_scaled)?:([A-Za-z0-9_.-]+)", nt):      # split rows rebuilt from a boundary the split override touched or rescaled (src=...|rc2f[_est|_scaled]:<id>)
            cands.append((t, "split_source_tag"))
        exd = int(_days(np.array([r.exDate], dtype="datetime64[ns]"))[0])
        for t, role in ((int(r.tradingItemId), "same_id"), (int(r.childTradingItemId), "child_id")):
            for o, h in sc.lookup_id(t, exd):
                cands.append((o, f"{role}_window:{h}"))
        if rules:
            if r.change == "added" and str(r.sourceNew) == "stitched_copy":
                cands.append(("RULE-X3-1", "rule:reassigned_row_mirrored_to_original_id(stitched_copy)"))
            if str(r.sourceOld) == "vendor_implied" or str(r.sourceNew) == "vendor_implied":
                cands.append(("RULE-X3-3", "rule:vendor_step_marker_window(prevDate,date]"))
            if (int(r.tradingItemId),) + amt_key(r) in moved_ids:
                cands.append(("RULE-X3-4", "rule:transition_leg_attributed_to_trading_successor"))
            if r.change == "changed" and r.columns == "status" and str(r.sourceNew) == "lineage":
                cands.append(("RULE-X5-link", "rule:lineage_row_status(chainable/ratioSource)"))
            if "exDate_aligned_to_settlement_bar" in nt:
                cands.append(("RULE-X4-align", "rule:value_leg_moved_to_settlement_bar"))
            elif (int(r.tradingItemId), r.legType, int(r.childTradingItemId), pd.Timestamp(r.exDate)) in aligned_from:
                cands.append(("RULE-X4-align", "rule:value_leg_moved_to_settlement_bar(source_date)"))
        oids = list(dict.fromkeys(o for o, _ in cands)); hows = list(dict.fromkeys(h for _, h in cands))
        out_oid.append(";".join(oids)); out_how.append(";".join(hows)); out_primary.append(oids[0] if oids else ""); out_n.append(len(oids))
    ld = ld.copy()
    ld["attributedTo"] = out_oid
    ld["how"] = out_how
    ld["attributedPrimary"] = out_primary
    ld["nAttributions"] = out_n
    if rules:
        # stitched copies inherit the attribution of their keeper (e.g. a copy re-pointed to a moved row)
        by_eid = {str(e): (a, h, p) for e, a, h, p in zip(ld.eventIdNew, ld.attributedTo, ld.how, ld.attributedPrimary) if a}
        for i in ld.index[(ld.attributedTo == "") & (ld.sourceNew.astype(str) == "stitched_copy") & (ld.keeperNew.astype(str) != "")]:
            k = str(ld.at[i, "keeperNew"])
            if k in by_eid:
                ld.at[i, "attributedTo"], ld.at[i, "how"], ld.at[i, "attributedPrimary"] = by_eid[k][0], "stitched_copy_of:" + by_eid[k][1], by_eid[k][2]
                ld.at[i, "nAttributions"] = len(by_eid[k][0].split(";"))
    return ld


def rule_scopes(extra: Scope, obs: ObsDates, ledger_new: pd.DataFrame, lineage_new: pd.DataFrame, lifecycle_new: pd.DataFrame) -> dict:
    """rc2f 규칙 변경의 셀 창: RULE-X5-link = rename/relist 쌍의 새 id 첫 행과 링크 행(옛 id 의 lifecycle 컷 마지막 관측 이후 첫 새 id 행) —
    이 셀들은 규칙 변경만으로 값/flag 가 바뀔 수 있다(NaN(unlinked) 포함). (RULE-X3-5 정산 현금 규칙은 R-X0 P0 로 제거: 셀 창 없음.)"""
    counts = {"x5_pairs": 0}
    ltd = lifecycle_new.set_index("tradingItemId").lastTradeDate
    ln = lineage_new[lineage_new.relation.isin(["rename", "relist"])]
    for r in ln.itertuples(index=False):
        old, new = int(r.oldId), int(r.newId)
        f = obs.first_day(new)
        if f is None:
            continue
        extra.add_window(new, f, f, "RULE-X5-link", "rule:link_first_row(chainable/after_old_last)")
        lo = obs.last_day(old)
        lt = pd.to_datetime(ltd.get(old, pd.NaT))
        if lo is not None and pd.notna(lt):
            lo = min(lo, int(_days(np.array([lt], dtype="datetime64[ns]"))[0]))
        if lo is not None and lo >= f:
            d1 = obs.first_on_or_after(new, lo + 1)
            if d1 is not None:
                extra.add_window(new, d1, d1, "RULE-X5-link", "rule:link_row_after_old_last")
        counts["x5_pairs"] += 1
    return counts


def attribute_cells(diff: pd.DataFrame, sc: Scope, ld: pd.DataFrame, obs: ObsDates, rules: bool = True, ledger_new: pd.DataFrame | None = None,
                    lineage_new: pd.DataFrame | None = None, lifecycle_new: pd.DataFrame | None = None) -> pd.DataFrame:
    """셀 → overrideId: 직접 창 > 귀속된 원장 변경 행의 창(같은 id, exDate..다음 관측일; rename/relist 는 새 id 첫 행) > (rules) 규칙 창.
    일치하는 창을 전부 기록(attributedTo ';' 결합, attributedPrimary = 우선순위 첫 항목, nAttributions; R-X0 P2-6)."""
    # 원장 변경 행 창
    extra = Scope()
    if rules and ledger_new is not None:
        rule_scopes(extra, obs, ledger_new, lineage_new, lifecycle_new)
    for r in ld[ld.attributedTo != ""].itertuples(index=False):
        tid = int(r.tradingItemId); day = int(_days(np.array([r.exDate], dtype="datetime64[ns]"))[0])
        d1 = obs.first_on_or_after(tid, day)
        d2 = obs.first_on_or_after(tid, d1 + 1) if d1 is not None else None
        extra.add_window(tid, day, d2 if d2 is not None else (d1 if d1 is not None else day), r.attributedTo, f"ledger_row_{r.change}({r.eventType})")
        if r.eventType in ("rename", "relist") and int(r.childTradingItemId) > 0:
            f = obs.first_day(int(r.childTradingItemId))
            if f is not None:
                extra.add_window(int(r.childTradingItemId), f, f, r.attributedTo, "ledger_link_row->new_first_row")
        for succ in sc.links.get(tid, ()):
            f = obs.first_day(succ)
            if f is not None:
                extra.add_window(succ, f, f, r.attributedTo, "ledger_row->linked_first_row")
    extra.build()
    oids, hows, prim, nn = [], [], [], []
    for tid, day in zip(diff.tradingItemId.to_numpy(), diff.day.to_numpy()):
        hit = sc.lookup(int(tid), int(day)) + extra.lookup(int(tid), int(day))       # direct windows first, then ledger-row/rule windows
        if hit:
            u = list(dict.fromkeys(o for o, _ in hit))
            oids.append(";".join(u)); hows.append(";".join(dict.fromkeys(h for _, h in hit))); prim.append(u[0]); nn.append(len(u))
        else:
            oids.append(""); hows.append(""); prim.append(""); nn.append(0)
    d = diff.copy()
    d["attributedTo"] = oids
    d["how"] = hows
    d["attributedPrimary"] = prim
    d["nAttributions"] = nn
    return d


def flag_transition(diff: pd.DataFrame, flags_old: dict, flags_new: dict) -> pd.DataFrame:
    """R-X0 P1-3: retFlag 전이 행렬(행 rc2 flag, 열 rc2f flag; 'added'/'removed' 포함). 바뀌지 않은 셀은 대각선(rc2 flag 수 − 바뀐 셀 수)."""
    labels = ["0", "1", "2", "3", "4", "added/removed"]
    m = pd.DataFrame(0, index=labels, columns=labels, dtype=np.int64)
    if len(diff):
        fo = np.where(diff.flagOld.to_numpy() < 0, 5, diff.flagOld.to_numpy()); fn = np.where(diff.flagNew.to_numpy() < 0, 5, diff.flagNew.to_numpy())
        ct = pd.crosstab(fo, fn)
        for i in ct.index:
            for j in ct.columns:
                m.iat[int(i), int(j)] = int(ct.at[i, j])
    for f in range(5):
        changed_from = int(m.iloc[f].sum())
        m.iat[f, f] += max(int(flags_old.get(f, 0)) - changed_from, 0)
    m.index.name = "rc2 \\ rc2f"
    return m


# --------------------------------------------------------------------------------------
# 레지스트리 재집계
# --------------------------------------------------------------------------------------
def window_stats(ret: dict, tid: int, d0: int, d1: int) -> tuple[int, int, float]:
    """(id, [d0, d1]) 창의 행 수, max retFlag, 첫 |ret| 최대."""
    k0, k1 = _keys([tid], [d0])[0], _keys([tid], [d1])[0]
    a, b = np.searchsorted(ret["key"], k0, side="left"), np.searchsorted(ret["key"], k1, side="right")
    if b <= a:
        return 0, -1, np.nan
    return int(b - a), int(ret["flag"][a:b].max()), float(np.nanmax(np.abs(ret["ret"][a:b]))) if np.isfinite(ret["ret"][a:b]).any() else np.nan


def registry_after(cases: pd.DataFrame, alog: pd.DataFrame, old: dict, new: dict, diff: pd.DataFrame, ledger_old: pd.DataFrame, ledger_new: pd.DataFrame, obs: ObsDates) -> pd.DataFrame:
    by_case = {}
    for r in alog.itertuples(index=False):
        by_case.setdefault(str(r.caseId), []).append((r.overrideId, r.status))
    diff_keys = set(zip(diff.tradingItemId.astype(int), diff.day.astype(int))) if len(diff) else set()
    diff_by_id = {}
    for t, d in diff_keys:
        diff_by_id.setdefault(t, []).append(d)
    lo = ledger_old.assign(ex=pd.to_datetime(ledger_old.exDate)); ln_ = ledger_new.assign(ex=pd.to_datetime(ledger_new.exDate))
    st_old = {k: ",".join(sorted(g.status)) for k, g in lo.groupby([lo.tradingItemId, lo.ex.dt.strftime("%Y-%m-%d")])}
    st_new = {k: ",".join(sorted(g.status)) for k, g in ln_.groupby([ln_.tradingItemId, ln_.ex.dt.strftime("%Y-%m-%d")])}
    rows = []
    for r in cases.itertuples(index=False):
        tid = int(r.tradingItemId)
        d0 = pd.Timestamp(r.impactFrom) if pd.notna(r.impactFrom) and str(r.impactFrom) else pd.Timestamp(r.eventDate)
        d1 = pd.Timestamp(r.impactTo) if pd.notna(r.impactTo) and str(r.impactTo) else d0
        dd0, dd1 = int(_days(np.array([d0], dtype="datetime64[ns]"))[0]), int(_days(np.array([d1], dtype="datetime64[ns]"))[0])
        nxt = obs.first_on_or_after(tid, dd1)
        dd1e = max(dd1, nxt) if nxt is not None else dd1
        n_o, f_o, _ = window_stats(old, tid, dd0, dd1e)
        n_n, f_n, _ = window_stats(new, tid, dd0, dd1e)
        ch = [d for d in diff_by_id.get(tid, []) if dd0 <= d <= dd1e]
        ov = by_case.get(str(r.caseId), [])
        ek = (tid, pd.Timestamp(r.eventDate).strftime("%Y-%m-%d")) if pd.notna(r.eventDate) and str(r.eventDate) else None
        rows.append({"caseId": r.caseId, "overrideIds": ";".join(o for o, _ in ov), "overrideStatus": ";".join(s for _, s in ov),
                     "rowsBefore": n_o, "rowsAfter": n_n, "maxRetFlagBefore": f_o, "maxRetFlagAfter": f_n,
                     "flag4Before": bool(f_o == 4), "flag4After": bool(f_n == 4), "cellsChanged": len(ch),
                     "ledgerStatusBefore": st_old.get(ek, "") if ek else "", "ledgerStatusAfter": st_new.get(ek, "") if ek else "",
                     "changed": bool(len(ch) or (f_o != f_n) or (n_o != n_n))})
    out = cases.reset_index(drop=True).copy()
    for c in ("overrideIds", "overrideStatus", "rowsBefore", "rowsAfter", "maxRetFlagBefore", "maxRetFlagAfter", "flag4Before", "flag4After", "cellsChanged",
              "ledgerStatusBefore", "ledgerStatusAfter", "changed"):
        out[c] = [x[c] for x in rows]
    return out


def flag4_after(f4: pd.DataFrame, new: dict, old: dict, diff: pd.DataFrame) -> pd.DataFrame:
    keys = _keys(f4.tradingItemId.astype(int).to_numpy(), _days(pd.to_datetime(f4.date).to_numpy()))
    pos = np.searchsorted(new["key"], keys)
    posc = np.minimum(pos, max(len(new["key"]) - 1, 0))
    hit = (len(new["key"]) > 0) & (new["key"][posc] == keys)
    out = f4.copy()
    out["retAfter"] = np.where(hit, new["ret"][posc], np.nan)
    out["retFlagAfter"] = np.where(hit, new["flag"][posc], -1)
    out["rowPresentAfter"] = hit
    ret_old = out.ret.astype(float).to_numpy()
    out["changed"] = hit & ((out.retFlagAfter.to_numpy() != out.retFlag.astype(int).to_numpy()) | (np.abs(np.nan_to_num(out.retAfter.to_numpy() - ret_old, nan=0.0)) > RET_TOL)) | ~hit
    out["resolved"] = hit & (out.retFlagAfter.to_numpy() != 4)
    out["stateAfter"] = np.where(~hit, "row_removed", np.where(np.isnan(out.retAfter.to_numpy()), "nan(unlinked)", np.where(out.retFlagAfter == 4, "still_flag4", "flag" + out.retFlagAfter.astype(str))))
    return out


# --------------------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rc2", type=Path, default=SP / "build_rc2_final")
    ap.add_argument("--build", type=Path, required=True, help="rc2f 재생성 빌드(publish 출력: derived/returns_total.parquet, events/ca_ledger.parquet, master/)")
    ap.add_argument("--stage", type=Path, required=True, help="rc2f_apply 출력(rc2f_apply_log.csv, overrides/, R1/events/ca_ledger_stitched_copies.csv)")
    ap.add_argument("--registry", type=Path, default=SP / "orch_rc2f" / "registry")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--no-gate", action="store_true", help="귀속 실패해도 exit 0")
    ap.add_argument("--rc2-rules", action="store_true", help="빌드가 rc2 규칙(--rc2-rules/--legacy-links)으로 생성됨: 규칙 변경 귀속 없이 교정만으로 귀속")
    a = ap.parse_args(argv)
    t0 = time.time()
    out = a.out; out.mkdir(parents=True, exist_ok=True)
    alog_p = a.stage / "rc2f_apply_log.csv"
    alog = pd.read_csv(alog_p, dtype=str, keep_default_na=False) if alog_p.exists() else pd.DataFrame(columns=["overrideId", "caseId", "kind", "action", "status", "reason", "affected_rows", "target", "file", "eventIds"])
    overrides = {}
    for p in sorted((a.stage / "overrides").glob("*.csv")) if (a.stage / "overrides").exists() else []:
        name = p.name.split("__", 1)[-1]
        kind = next((k for k in ("split", "price", "ledger", "lifecycle", "lineage", "aux") if name.startswith(k + "_")), None)
        if kind:
            df = pd.read_csv(p, dtype=str, keep_default_na=False)
            overrides[kind] = pd.concat([overrides[kind], df], ignore_index=True) if kind in overrides else df
    old = load_returns(a.rc2 / "derived" / "returns_total.parquet")
    new = load_returns(a.build / "derived" / "returns_total.parquet")
    print(f"returns loaded: rc2 {len(old['key'])} rows, new {len(new['key'])} rows ({time.time() - t0:.0f}s)", flush=True)
    diff = returns_diff(old, new)
    obs = ObsDates(old, new)
    ledger_old = pd.read_parquet(a.rc2 / "events" / "ca_ledger.parquet")
    ledger_new = pd.read_parquet(a.build / "events" / "ca_ledger.parquet")
    lineage_old = pd.read_parquet(a.rc2 / "master" / "lineage.parquet"); lineage_new = pd.read_parquet(a.build / "master" / "lineage.parquet")
    lc_old = pd.read_parquet(a.rc2 / "master" / "lifecycle.parquet", columns=["tradingItemId", "lastTradeDate"])
    lc_new = pd.read_parquet(a.build / "master" / "lifecycle.parquet", columns=["tradingItemId", "lastTradeDate"])
    st_p = a.stage / "R1" / "events" / "ca_ledger_stitched_copies.csv"
    stitched = pd.read_csv(st_p) if st_p.exists() else None
    ld = ledger_diff(ledger_old, ledger_new)
    sc = build_scope(alog, overrides, obs, ledger_old, ledger_new, lineage_old, lineage_new, lc_old, lc_new, stitched)
    ld = attribute_ledger_rows(ld, sc, ledger_new, ledger_old, rules=not a.rc2_rules, lineage=lineage_new)
    diff = attribute_cells(diff, sc, ld, obs, rules=not a.rc2_rules, ledger_new=ledger_new, lineage_new=lineage_new, lifecycle_new=lc_new)
    diff.to_parquet(out / "returns_diff.parquet", index=False)
    ld.to_csv(out / "ledger_diff.csv", index=False)
    un_cells = diff[diff.attributedTo == ""]
    un_rows = ld[ld.attributedTo == ""]
    un_cells.head(5000).to_csv(out / "unattributed_cells.csv", index=False)
    un_rows.to_csv(out / "unattributed_ledger_rows.csv", index=False)
    print(f"returns diff: {len(diff)} cells ({diff.kind.value_counts().to_dict() if len(diff) else {}}), unattributed {len(un_cells)}; ledger diff {len(ld)} rows, unattributed {len(un_rows)} ({time.time() - t0:.0f}s)", flush=True)
    # 레지스트리
    cases = pd.read_csv(a.registry / "registry_cases_v0.csv", dtype={"caseId": str}, keep_default_na=False)
    for c in ("eventDate", "impactFrom", "impactTo"):
        cases[c] = pd.to_datetime(cases[c].replace("", np.nan), errors="coerce")
    ra = registry_after(cases, alog, old, new, diff, ledger_old, ledger_new, obs)
    ra.to_csv(out / "registry_after.csv", index=False)
    f4 = pd.read_csv(a.registry / "flag4_rows_with_cause.csv", keep_default_na=False)
    fa = flag4_after(f4, new, old, diff)
    fa.to_csv(out / "flag4_after.csv", index=False)
    # 요약
    flags_new = {int(k): int(v) for k, v in zip(*np.unique(new["flag"], return_counts=True))}
    flags_old = {int(k): int(v) for k, v in zip(*np.unique(old["flag"], return_counts=True))}
    resid = fa[~fa.resolved]
    by_cause_before = f4.cause.str.split(":").str[0].value_counts().to_dict() if "cause" in f4 else {}
    by_cause_after = resid.cause.str.split(":").str[0].value_counts().to_dict() if "cause" in resid else {}
    by_cause_full_after = resid.cause.value_counts().to_dict() if "cause" in resid else {}
    lists_before, lists_after = {}, {}
    for r in ra.itertuples(index=False):
        for l in str(r.lists).split(";"):
            l = l.strip()
            if not l:
                continue
            lists_before[l] = lists_before.get(l, 0) + 1
            if not r.changed:
                lists_after[l] = lists_after.get(l, 0) + 1
    apply_counts = alog.groupby(["kind", "status"]).size().unstack(fill_value=0).to_dict("index") if len(alog) else {}
    gate = {"unattributed_cells": int(len(un_cells)), "unattributed_ledger_rows": int(len(un_rows)), "pass": bool(len(un_cells) == 0 and len(un_rows) == 0)}
    trans = flag_transition(diff, flags_old, flags_new)
    new4 = diff[(diff.flagNew == 4) & (diff.flagOld != 4)] if len(diff) else diff
    new4_by_how = new4.how.str.split(";").str[0].value_counts().to_dict() if len(new4) else {}
    new4_by_oid = new4.attributedPrimary.value_counts().to_dict() if len(new4) else {}
    multi = {"cells_multi_attributed": int((diff.nAttributions > 1).sum()) if len(diff) else 0,
             "ledger_rows_multi_attributed": int((ld.nAttributions > 1).sum()) if len(ld) else 0,
             "ledger_rows_same_id_window": int(ld.how.str.contains("same_id_window|child_id_window").sum()) if len(ld) else 0,
             "ledger_rows_by_primary_how": ld[ld.attributedTo != ""].how.str.split(";").str[0].str.split(":").str[0].value_counts().to_dict() if len(ld) else {},
             "cells_by_primary_how": diff[diff.attributedTo != ""].how.str.split(";").str[0].value_counts().head(30).to_dict() if len(diff) else {}}
    summary = {
        "rc2": str(a.rc2), "build": str(a.build), "stage": str(a.stage), "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "overrides": {"total": int(len(alog)), "by_kind_status": apply_counts},
        "returns": {"rows_rc2": int(len(old["key"])), "rows_new": int(len(new["key"])), "retFlag_rc2": flags_old, "retFlag_new": flags_new,
                    "changed_cells": int(len(diff)), "changed_by_kind": diff.kind.value_counts().to_dict() if len(diff) else {},
                    "changed_by_how": diff.how.value_counts().head(30).to_dict() if len(diff) else {}},
        "ledger": {"rows_rc2": int(len(ledger_old)), "rows_new": int(len(ledger_new)), "changed_rows": int(len(ld)), "changed_by_change": ld.change.value_counts().to_dict() if len(ld) else {},
                   "status_rc2": ledger_old[ledger_old.source != "stitched_copy"].status.value_counts().to_dict(), "status_new": ledger_new[ledger_new.source != "stitched_copy"].status.value_counts().to_dict()},
        "retFlag_transition": {"labels": list(trans.index), "matrix": trans.to_numpy().tolist(),
                               "new_flag4_cells": int(len(new4)), "new_flag4_by_how": new4_by_how, "new_flag4_by_override": new4_by_oid},
        "attribution_detail": multi,
        "flag4_584": {"rows": int(len(fa)), "resolved": int(fa.resolved.sum()), "residual": int((~fa.resolved).sum()), "row_removed": int((~fa.rowPresentAfter).sum()),
                      "residual_by_cause": by_cause_after, "before_by_cause": by_cause_before, "residual_by_cause_detail": by_cause_full_after,
                      "residual_state": resid.stateAfter.value_counts().to_dict() if len(resid) else {},
                      "resolved_state": fa[fa.resolved].stateAfter.value_counts().to_dict() if fa.resolved.any() else {},
                      "resolved_by_cause": fa[fa.resolved].cause.str.split(":").str[0].value_counts().to_dict() if ("cause" in fa and fa.resolved.any()) else {}},
        "registry": {"cases": int(len(ra)), "cases_with_override": int((ra.overrideIds != "").sum()), "cases_changed": int(ra.changed.sum()),
                     "flag4_cases_before": int(ra.flag4Before.sum()), "flag4_cases_after": int(ra.flag4After.sum()),
                     "by_primaryClass_flag4_before": ra[ra.flag4Before].primaryClass.value_counts().to_dict(), "by_primaryClass_flag4_after": ra[ra.flag4After].primaryClass.value_counts().to_dict(),
                     "lists_cases_total": lists_before, "lists_cases_unchanged": lists_after},
        "attribution_gate": gate, "attributed_cells_by": diff[diff.attributedTo != ""].attributedTo.str.split(";").explode().value_counts().head(40).to_dict() if len(diff) else {},
        "attributed_ledger_rows_by": ld[ld.attributedTo != ""].attributedTo.str.split(";").explode().value_counts().head(40).to_dict() if len(ld) else {},
        "rules": "rc2(no rule attribution)" if a.rc2_rules else "rc2f(RULE-X3-1/3/4, RULE-X4-align, RULE-X5-link)",
        "seconds": round(time.time() - t0, 1), "peak_rss_gb": _rss_gb(),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    md = [f"# rc2f recount ({summary['at']})", "", f"rc2 `{a.rc2}` vs build `{a.build}`", "",
          "## 1. 교정 적용", "", "| kind | " + " | ".join(sorted({s for v in apply_counts.values() for s in v})) + " |", "|---|" + "---|" * len({s for v in apply_counts.values() for s in v})]
    sts = sorted({s for v in apply_counts.values() for s in v})
    for k, v in apply_counts.items():
        md.append(f"| {k} | " + " | ".join(str(v.get(s, 0)) for s in sts) + " |")
    md += ["", "## 2. returns 변화", "", "| 항목 | rc2 | rc2f |", "|---|---|---|", f"| rows | {len(old['key'])} | {len(new['key'])} |"]
    for f in range(5):
        md.append(f"| retFlag {f} | {flags_old.get(f, 0)} | {flags_new.get(f, 0)} |")
    md += [f"| changed cells | | {len(diff)} ({summary['returns']['changed_by_kind']}) |", "",
           "### 2b. retFlag 전이 행렬 (행 rc2, 열 rc2f; 바뀌지 않은 셀은 대각선)", "",
           "| rc2 \\ rc2f | " + " | ".join(trans.columns) + " |", "|---|" + "---|" * len(trans.columns)]
    for lab, row in zip(trans.index, trans.to_numpy()):
        md.append(f"| {lab} | " + " | ".join(str(int(x)) for x in row) + " |")
    md += ["", f"새로 flag 4 가 된 셀 {len(new4)} (귀속 how 별 {new4_by_how}; override 별 {new4_by_oid})", "",
           f"다중 귀속: 셀 {multi['cells_multi_attributed']}, 원장 행 {multi['ledger_rows_multi_attributed']} (같은 id 창 귀속 원장 행 {multi['ledger_rows_same_id_window']})", "",
           "## 3. flag 4 (584 행)", "", "| | 수 |", "|---|---|", f"| rows | {len(fa)} |", f"| resolved (retFlag != 4) | {int(fa.resolved.sum())} |",
           f"| resolved by state | {summary['flag4_584']['resolved_state']} |",
           f"| residual | {int((~fa.resolved).sum())} |", f"| residual by state | {summary['flag4_584']['residual_state']} |",
           f"| residual by cause | {by_cause_after} |", f"| before by cause | {by_cause_before} |", "",
           "## 4. 레지스트리", "", f"cases {len(ra)}, with override {int((ra.overrideIds != '').sum())}, changed {int(ra.changed.sum())}, flag4 cases before {int(ra.flag4Before.sum())} → after {int(ra.flag4After.sum())}", "",
           "## 5. 귀속 게이트", "", f"unattributed cells {len(un_cells)}, unattributed ledger rows {len(un_rows)} → **{'PASS' if gate['pass'] else 'FAIL'}**", ""]
    if len(un_cells):
        md += ["### unattributed cells (first 30)", "", un_cells.head(30).to_string(index=False), ""]
    if len(un_rows):
        md += ["### unattributed ledger rows (first 30)", "", un_rows.head(30).to_string(index=False), ""]
    (out / "summary.md").write_text("\n".join(md))
    print(json.dumps({k: v for k, v in summary.items() if k not in ("registry",)}, indent=1, default=str))
    if not gate["pass"] and not a.no_gate:
        print("ATTRIBUTION GATE FAIL", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
