#!/usr/bin/env python3
"""Refresh the local USS cash master from the xfeed source database.

The vendor collector (``sim/vendor/uss/save_uss_master_market_data.py``) joins
``ciqpriceequity`` on *today's* pricing date, so a security that stopped trading
never comes back in a daily snapshot and its ``securityEndDate`` stays null
forever.  This tool re-reads the identity fields straight from the source
tables - no price join at all - for every ``tradingItemId`` already present in
the local master, and writes a refreshed copy next to the repository data
directory (``/home/base`` is read-only).

Updated fields:
  ciqsecurity.securityenddate / securitystartdate
  ciqtradingitem.tickersymbol / exchangeid / tradingitemstatusid
  ciqcompany.companyname

Schema of the output = schema of the input (same index, same column order and
dtypes) plus the bookkeeping columns ``tradingItemStatusId``, ``refreshedAt``,
``endDateChanged``, ``tickerChanged``, ``exchangeChanged`` and
``endDateConflict``.

Safety rules (brief P2-0):
  * DB credentials are parsed out of the vendor collector - never hard-coded
    here - and never printed.
  * The session is opened read-only twice over (connection option and
    ``set_session``) with an explicit statement timeout.
  * An existing non-null ``securityEndDate`` is never overwritten; a
    disagreement with the database is only *flagged* (``endDateConflict``).

Re-running the tool is idempotent: the same input yields the same output apart
from ``refreshedAt``, and running it again on its own output keeps the change
flags (they are OR-ed with the previous run).
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Sequence

import numpy as np
import pandas as pd

VENDOR_COLLECTOR = Path("/home/base/git/sim/vendor/uss/save_uss_master_market_data.py")
DEFAULT_MASTER = Path("/home/base/vendor_data/uss/cash/master/uss_cash_master_acc.parquet")
DEFAULT_OUT = Path("/home/jgo/longshort_platform/data/master/uss_cash_master_refreshed.parquet")
DEFAULT_UNIVERSE_DIR = Path("/home/base/cache/cache_v1_cash_1d/TopUniverseV1")
UNIVERSES = ("Top500", "Top1000", "ALL")
UNIVERSE_SINCE = "2010-01-01"

# Bookkeeping columns appended to the vendor schema.
EXTRA_COLUMNS = [
    "tradingItemStatusId",
    "refreshedAt",
    "endDateChanged",
    "tickerChanged",
    "exchangeChanged",
    "endDateConflict",
]
FLAG_COLUMNS = ["endDateChanged", "tickerChanged", "exchangeChanged", "endDateConflict"]

QUERY = """
select ti.tradingitemid        as trading_item_id,
       ti.tickersymbol         as ticker_symbol,
       ti.exchangeid           as exchange_id,
       ti.tradingitemstatusid  as trading_item_status_id,
       s.securitystartdate     as security_start_date,
       s.securityenddate       as security_end_date,
       s.companyid             as company_id,
       c.companyname           as company_name
from xfl_target.ciqtradingitem ti
join xfl_target.ciqsecurity s on s.securityid = ti.securityid
left join xfl_target.ciqcompany c on c.companyid = s.companyid
where ti.tradingitemid = any(%s)
"""


STATUS_QUERY = "select tradingitemstatusid, tradingitemstatusname from xfl_target.ciqtradingitemstatus"


# --------------------------------------------------------------------------- db


def load_db_constants(source: Path = VENDOR_COLLECTOR) -> Dict[str, str]:
    """Parse the ``DB_*`` module constants of the vendor collector.

    The file is parsed with ``ast`` (not imported) so none of its heavy
    third-party imports or side effects run.
    """
    tree = ast.parse(source.read_text())
    consts: Dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id.startswith("DB_") and isinstance(node.value, ast.Constant):
                consts[target.id] = node.value.value
    missing = {"DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_PORT"} - set(consts)
    if missing:
        raise RuntimeError(f"{source}: missing DB constants {sorted(missing)}")
    return consts


def connect(consts: Dict[str, str], statement_timeout_ms: int):
    """Open a strictly read-only connection to xfeed."""
    import psycopg2  # imported lazily so --help works without the driver

    conn = psycopg2.connect(
        host=consts["DB_HOST"],
        dbname=consts["DB_NAME"],
        user=consts["DB_USER"],
        password=consts["DB_PASSWORD"],
        port=consts["DB_PORT"],
        options=f"-c default_transaction_read_only=on -c statement_timeout={int(statement_timeout_ms)}",
    )
    conn.set_session(readonly=True)
    return conn


def fetch_identity(conn, ids: Sequence[int], batch_size: int, verbose: bool = True) -> pd.DataFrame:
    """Fetch identity rows for ``ids`` in ``= any(%s)`` batches (no price join)."""
    collected: List[tuple] = []
    cols = [
        "trading_item_id",
        "ticker_symbol",
        "exchange_id",
        "trading_item_status_id",
        "security_start_date",
        "security_end_date",
        "company_id",
        "company_name",
    ]
    cur = conn.cursor()
    try:
        for start in range(0, len(ids), batch_size):
            chunk = [int(x) for x in ids[start : start + batch_size]]
            cur.execute(QUERY, (chunk,))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            collected.extend(rows)
            if verbose:
                done = min(start + batch_size, len(ids))
                print(f"  fetched {done}/{len(ids)} ids ({len(rows)} rows in batch)", flush=True)
    finally:
        cur.close()
    db = pd.DataFrame(collected, columns=cols)
    if not db.empty and db["trading_item_id"].duplicated().any():
        dups = db.loc[db["trading_item_id"].duplicated(), "trading_item_id"].unique()[:10]
        raise RuntimeError(f"duplicate tradingItemId rows from xfeed: {list(dups)}")
    db["security_start_date"] = pd.to_datetime(db["security_start_date"], errors="coerce")
    db["security_end_date"] = pd.to_datetime(db["security_end_date"], errors="coerce")
    db["_found"] = True
    return db.set_index("trading_item_id")


def fetch_status_names(conn) -> Dict[int, str]:
    """tradingitemstatusid -> human readable name (report only)."""
    with conn.cursor() as cur:
        cur.execute(STATUS_QUERY)
        return {int(k): v for k, v in cur.fetchall()}


# ---------------------------------------------------------------------- refresh


@dataclass
class RefreshStats:
    n_rows: int
    n_queried: int
    n_found: int
    n_missing_in_db: int
    end_date_filled: int
    end_date_conflict: int
    end_date_db_null_local_set: int
    ticker_changed: int
    ticker_filled: int
    exchange_changed: int
    company_name_changed: int
    start_date_changed: int
    status_id_set: int


def _prev_flag(master: pd.DataFrame, name: str) -> pd.Series:
    if name in master.columns:
        return master[name].fillna(False).astype(bool)
    return pd.Series(False, index=master.index)


def refresh_master(master: pd.DataFrame, db: pd.DataFrame, refreshed_at: pd.Timestamp):
    """Return (refreshed frame, stats, change detail frame)."""
    out = master.copy()
    aligned = db.reindex(master.index)
    found = pd.Series(master.index.isin(db.index), index=master.index)

    # --- securityEndDate: fill nulls only, flag disagreements -----------------
    local_end = pd.to_datetime(master["securityEndDate"])
    db_end = aligned["security_end_date"]
    fill_end = local_end.isna() & db_end.notna()
    conflict = local_end.notna() & db_end.notna() & (local_end != db_end)
    db_null_local_set = local_end.notna() & found & db_end.isna()
    new_end = local_end.where(~fill_end, db_end)

    # --- tickersymbol ---------------------------------------------------------
    local_sym = master["symbol"]
    db_sym = aligned["ticker_symbol"]
    sym_present = db_sym.notna()
    sym_differs = sym_present & (local_sym.fillna("") != db_sym.fillna(""))
    ticker_filled = sym_differs & local_sym.isna()
    new_sym = local_sym.where(~sym_differs, db_sym)

    # --- exchangeId -----------------------------------------------------------
    local_exch = master["exchangeId"]
    db_exch = pd.to_numeric(aligned["exchange_id"], errors="coerce")
    exch_differs = db_exch.notna() & (local_exch != db_exch)
    new_exch = pd.Series(np.where(exch_differs, db_exch, local_exch), index=master.index).astype(
        master["exchangeId"].dtype
    )

    # --- companyName ----------------------------------------------------------
    local_name = master["companyName"]
    db_name = aligned["company_name"]
    name_differs = db_name.notna() & (local_name.fillna("") != db_name.fillna(""))
    new_name = local_name.where(~name_differs, db_name)

    # --- securityStartDate (string 'YYYY-MM-DD' in the vendor schema) ---------
    local_start = master["securityStartDate"]
    db_start_str = aligned["security_start_date"].dt.strftime("%Y-%m-%d")
    start_differs = db_start_str.notna() & (local_start.fillna("") != db_start_str.fillna(""))
    new_start = local_start.where(~start_differs, db_start_str)

    out["securityEndDate"] = new_end.astype("datetime64[ns]")
    out["symbol"] = new_sym
    out["exchangeId"] = new_exch
    out["companyName"] = new_name
    out["securityStartDate"] = new_start

    status = pd.to_numeric(aligned["trading_item_status_id"], errors="coerce").astype("Int64")
    out["tradingItemStatusId"] = status
    out["refreshedAt"] = refreshed_at
    out["endDateChanged"] = (fill_end | _prev_flag(master, "endDateChanged")).astype(bool)
    out["tickerChanged"] = (sym_differs | _prev_flag(master, "tickerChanged")).astype(bool)
    out["exchangeChanged"] = (exch_differs | _prev_flag(master, "exchangeChanged")).astype(bool)
    out["endDateConflict"] = (conflict | _prev_flag(master, "endDateConflict")).astype(bool)

    base_cols = [c for c in master.columns if c not in EXTRA_COLUMNS]
    out = out[base_cols + EXTRA_COLUMNS]

    stats = RefreshStats(
        n_rows=len(master),
        n_queried=int(found.shape[0]),
        n_found=int(found.sum()),
        n_missing_in_db=int((~found).sum()),
        end_date_filled=int(fill_end.sum()),
        end_date_conflict=int(conflict.sum()),
        end_date_db_null_local_set=int(db_null_local_set.sum()),
        ticker_changed=int((sym_differs & ~ticker_filled).sum()),
        ticker_filled=int(ticker_filled.sum()),
        exchange_changed=int(exch_differs.sum()),
        company_name_changed=int(name_differs.sum()),
        start_date_changed=int(start_differs.sum()),
        status_id_set=int(status.notna().sum()),
    )

    detail = pd.DataFrame(
        {
            "symbolOld": local_sym,
            "symbolNew": new_sym,
            "companyName": new_name,
            "exchangeIdOld": local_exch,
            "exchangeIdNew": new_exch,
            "endDateOld": local_end,
            "endDateNew": new_end,
            "endDateDb": db_end,
            "tradingItemStatusId": status,
            "endDateChanged": fill_end,
            "tickerChanged": sym_differs,
            "exchangeChanged": exch_differs,
            "endDateConflict": conflict,
            "foundInDb": found,
        }
    )
    # Cumulative flags (this run OR-ed with flags already stored in the input) so a
    # re-run over an already refreshed file still reports the full change set.
    for col in FLAG_COLUMNS:
        detail[col + "Cum"] = out[col].to_numpy()
    detail.index.name = master.index.name
    return out, stats, detail


# --------------------------------------------------------------------- universe


def universe_members(universe_dir: Path, since: str = UNIVERSE_SINCE) -> Dict[str, set]:
    """Union of members per universe from ``since`` onwards."""
    members: Dict[str, set] = {}
    for name in UNIVERSES:
        path = universe_dir / f"{name}.parquet"
        if not path.exists():
            print(f"  [warn] universe file not found: {path}", file=sys.stderr)
            continue
        flags = pd.read_parquet(path)
        flags = flags.loc[flags.index >= pd.Timestamp(since)]
        cols = flags.columns[flags.to_numpy(dtype=bool).any(axis=0)]
        members[name] = {int(c) for c in cols}
    return members


# ----------------------------------------------------------------------- report


def _fmt_date(value) -> str:
    if value is None or (isinstance(value, float) and np.isnan(value)) or pd.isna(value):
        return ""
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def _md_table(header: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
    for row in rows:
        out.append("| " + " | ".join("" if v is None else str(v) for v in row) + " |")
    return "\n".join(out)


def build_report(
    stats: RefreshStats,
    detail: pd.DataFrame,
    members: Dict[str, set],
    csv_paths: Dict[str, Path],
    master_path: Path,
    out_path: Path,
    refreshed_at: pd.Timestamp,
    check_ids: Dict[str, int],
    status_names: Dict[int, str] | None = None,
) -> str:
    status_names = status_names or {}
    filled = detail[detail["endDateChangedCum"]]
    lines: List[str] = []
    lines.append("# 마스터 종료일 갱신 보고 (refresh_master.py)")
    lines.append("")
    lines.append(f"- 실행 시각: {refreshed_at:%Y-%m-%d %H:%M:%S}")
    lines.append(f"- 입력: `{master_path}`")
    lines.append(f"- 출력: `{out_path}`")
    lines.append("- 조회 소스: xfeed `xfl_target.ciqtradingitem` ⋈ `ciqsecurity` ⟕ `ciqcompany` (가격 조인 없음, 읽기 전용 세션)")
    lines.append("")
    lines.append("## 1. 요약")
    lines.append("")
    lines.append(
        _md_table(
            ["항목", "건수"],
            [
                ["마스터 행수", f"{stats.n_rows:,}"],
                ["xfeed 조회 대상 id", f"{stats.n_queried:,}"],
                ["xfeed에서 찾은 id", f"{stats.n_found:,}"],
                ["xfeed에 없는 id", f"{stats.n_missing_in_db:,}"],
                ["**종료일 신규 채움(이번 실행)**", f"**{stats.end_date_filled:,}**"],
                ["종료일 채움 누적(`endDateChanged=True`)", f"{int(detail['endDateChangedCum'].sum()):,}"],
                ["종료일 불일치(로컬 유지, endDateConflict)", f"{stats.end_date_conflict:,}"],
                ["로컬만 종료일 있음(DB null)", f"{stats.end_date_db_null_local_set:,}"],
                ["티커 변경(기존 값 → 새 값, 이번 실행)", f"{stats.ticker_changed:,}"],
                ["티커 신규 채움(로컬 null, 이번 실행)", f"{stats.ticker_filled:,}"],
                ["티커 변경 누적(`tickerChanged=True`)", f"{int(detail['tickerChangedCum'].sum()):,}"],
                ["거래소 변경 누적(`exchangeChanged=True`)", f"{int(detail['exchangeChangedCum'].sum()):,}"],
                ["회사명 변경", f"{stats.company_name_changed:,}"],
                ["상장일(securityStartDate) 변경", f"{stats.start_date_changed:,}"],
                ["tradingItemStatusId 채워진 행", f"{stats.status_id_set:,}"],
            ],
        )
    )
    lines.append("")
    lines.append("종료일이 이미 있던 행은 **덮어쓰지 않음** — DB와 다르면 `endDateConflict=True`로 표시만 함.")
    lines.append("")

    lines.append("## 2. 채워진 종료일 연도별 분포")
    lines.append("")
    if filled.empty:
        lines.append("(없음)")
    else:
        years = filled["endDateNew"].dt.year.value_counts().sort_index()
        lines.append(_md_table(["연도", "건수"], [[int(y), int(n)] for y, n in years.items()] + [["**합계**", f"**{int(years.sum()):,}**"]]))
    lines.append("")

    lines.append("## 3. 변경 전후 샘플 10건 (종료일 채움, 종료일 내림차순)")
    lines.append("")
    if filled.empty:
        lines.append("(없음)")
    else:
        sample = filled.sort_values("endDateNew", ascending=False).head(10)
        lines.append(
            _md_table(
                ["tradingItemId", "symbol", "companyName", "종료일 이전", "종료일 이후", "statusId"],
                [
                    [
                        idx,
                        row["symbolNew"],
                        row["companyName"],
                        _fmt_date(row["endDateOld"]) or "(null)",
                        _fmt_date(row["endDateNew"]),
                        "" if pd.isna(row["tradingItemStatusId"]) else int(row["tradingItemStatusId"]),
                    ]
                    for idx, row in sample.iterrows()
                ],
            )
        )
    lines.append("")

    tick = detail[detail["tickerChangedCum"]]
    lines.append("## 4. 티커·거래소 변경 샘플")
    lines.append("")
    if tick.empty:
        lines.append("티커 변경 없음.")
    else:
        lines.append(
            _md_table(
                ["tradingItemId", "symbol 이전", "symbol 이후", "companyName", "거래소 이전", "거래소 이후"],
                [
                    [idx, r["symbolOld"], r["symbolNew"], r["companyName"], int(r["exchangeIdOld"]), int(r["exchangeIdNew"])]
                    for idx, r in tick.head(10).iterrows()
                ],
            )
        )
    lines.append("")
    exch = detail[detail["exchangeChangedCum"]]
    if not exch.empty:
        lines.append(
            _md_table(
                ["tradingItemId", "symbol", "거래소 이전", "거래소 이후", "종료일"],
                [
                    [idx, r["symbolNew"], int(r["exchangeIdOld"]), int(r["exchangeIdNew"]), _fmt_date(r["endDateNew"])]
                    for idx, r in exch.head(10).iterrows()
                ],
            )
        )
        lines.append("")

    lines.append("## 5. 유니버스별 변경 종목 (2010년 이후 합집합)")
    lines.append("")
    rows = []
    for name in UNIVERSES:
        ids = members.get(name)
        if ids is None:
            continue
        idx = detail.index.intersection(pd.Index(sorted(ids), name=detail.index.name))
        sub = detail.loc[idx]
        rows.append(
            [
                name,
                f"{len(ids):,}",
                f"{int(sub['endDateChangedCum'].sum()):,}",
                f"{int(sub['tickerChangedCum'].sum()):,}",
                f"{int(sub['exchangeChangedCum'].sum()):,}",
                f"{int(sub['endDateConflictCum'].sum()):,}",
                f"`{csv_paths[name].name}`" if name in csv_paths else "",
            ]
        )
    lines.append(_md_table(["유니버스", "구성종목(합집합)", "종료일 채움", "티커 변경", "거래소 변경", "종료일 불일치", "CSV"], rows))
    lines.append("")
    for name in UNIVERSES:
        ids = members.get(name)
        if ids is None:
            continue
        sub = detail.loc[detail.index.intersection(pd.Index(sorted(ids), name=detail.index.name))]
        sub = sub[sub["endDateChangedCum"]]
        if sub.empty:
            continue
        lines.append(f"### {name} 종료일 채움 {len(sub)}건 (상위 15건, 종료일 내림차순)")
        lines.append("")
        lines.append(
            _md_table(
                ["tradingItemId", "symbol", "companyName", "종료일"],
                [
                    [idx, r["symbolNew"], r["companyName"], _fmt_date(r["endDateNew"])]
                    for idx, r in sub.sort_values("endDateNew", ascending=False).head(15).iterrows()
                ],
            )
        )
        lines.append("")

    conflicts = detail[detail["endDateConflictCum"]]
    missing = detail[~detail["foundInDb"]]
    lines.append("## 6. 종료일 불일치·xfeed 미존재 (덮어쓰지 않음)")
    lines.append("")
    lines.append(f"- 종료일 불일치 {len(conflicts)}건: 로컬 값을 유지하고 `endDateConflict=True`로만 표시함.")
    if not conflicts.empty:
        lines.append("")
        lines.append(
            _md_table(
                ["tradingItemId", "symbol", "companyName", "로컬 종료일", "DB 종료일", "차이(일)"],
                [
                    [
                        idx,
                        r["symbolNew"],
                        r["companyName"],
                        _fmt_date(r["endDateOld"]),
                        _fmt_date(r["endDateDb"]),
                        int((pd.Timestamp(r["endDateDb"]) - pd.Timestamp(r["endDateOld"])).days),
                    ]
                    for idx, r in conflicts.sort_values("endDateOld", ascending=False).head(20).iterrows()
                ],
            )
        )
    lines.append("")
    lines.append(f"- xfeed `ciqtradingitem`에 없는 id {len(missing)}건: 로컬 값 그대로 둠.")
    if not missing.empty:
        lines.append("")
        lines.append(
            _md_table(
                ["tradingItemId", "symbol", "companyName", "종료일"],
                [[idx, r["symbolOld"], r["companyName"], _fmt_date(r["endDateOld"])] for idx, r in missing.iterrows()],
            )
        )
    lines.append("")
    if status_names:
        used = sorted({int(v) for v in detail["tradingItemStatusId"].dropna().unique()})
        counts = detail["tradingItemStatusId"].value_counts()
        lines.append("`tradingItemStatusId` 분포 (`ciqtradingitemstatus`):")
        lines.append("")
        lines.append(
            _md_table(
                ["statusId", "이름", "행수"],
                [[sid, status_names.get(sid, "?"), f"{int(counts.get(sid, 0)):,}"] for sid in used],
            )
        )
        lines.append("")

    lines.append("## 7. 검증 종목 (브리프 §3의 8종목)")
    lines.append("")
    rows = []
    for sym, tid in check_ids.items():
        if tid not in detail.index:
            rows.append([sym, tid, "마스터에 없음", "", "", ""])
            continue
        r = detail.loc[tid]
        rows.append(
            [
                sym,
                tid,
                _fmt_date(r["endDateOld"]) or "(null)",
                _fmt_date(r["endDateNew"]) or "(null)",
                "채움" if r["endDateChangedCum"] else ("유지" if pd.notna(r["endDateNew"]) else "없음"),
                "" if pd.isna(r["tradingItemStatusId"]) else int(r["tradingItemStatusId"]),
            ]
        )
    lines.append(_md_table(["symbol", "tradingItemId", "종료일 이전", "종료일 이후", "판정", "statusId"], rows))
    lines.append("")

    lines.append("## 8. 재현")
    lines.append("")
    lines.append("```")
    lines.append("/home/jgo/usr/venv-3.11/bin/python cpp-engine/tools/data/refresh_master.py \\")
    lines.append(f"  --master {master_path} \\")
    lines.append(f"  --out {out_path}")
    lines.append("```")
    lines.append("")
    lines.append(
        "추가 컬럼: `tradingItemStatusId`(ciqtradingitem, Int64) · `refreshedAt` · "
        "`endDateChanged` · `tickerChanged` · `exchangeChanged` · `endDateConflict`. "
        "기존 15컬럼·인덱스(tradingItemId int64)·dtype는 그대로임."
    )
    lines.append("")
    return "\n".join(lines)


# -------------------------------------------------------------------------- cli


CHECK_IDS = {
    "DFS": 34969516,
    "HES": 27143925,
    "ANSS": 2590144,
    "CYBR": 267935529,
    "EXAS": 2610459,
    "EA": 2608773,
    "AVB": 2591859,
    "EQR": 2610085,
}


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--master", type=Path, default=DEFAULT_MASTER, help="local master parquet (read-only)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="refreshed master parquet to write")
    ap.add_argument("--dry-run", action="store_true", help="query and report to stdout, write nothing")
    ap.add_argument("--batch-size", type=int, default=5000, help="ids per `= any(%%s)` batch (default 5000)")
    ap.add_argument("--universe-dir", type=Path, default=DEFAULT_UNIVERSE_DIR)
    ap.add_argument("--since", default=UNIVERSE_SINCE, help="universe membership start date (default 2010-01-01)")
    ap.add_argument(
        "--ids-from-cache",
        action="store_true",
        help="query only ids that are in a TopUniverseV1 universe since --since (faster partial refresh)",
    )
    ap.add_argument("--report", type=Path, default=None, help="report path (default <out dir>/refresh_report.md)")
    ap.add_argument("--statement-timeout-ms", type=int, default=600_000)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    master = pd.read_parquet(args.master)
    if master.index.name != "tradingItemId":
        raise RuntimeError(f"unexpected master index: {master.index.name!r}")
    if not master.index.is_unique:
        raise RuntimeError("master index has duplicate tradingItemId values")
    print(f"master: {args.master} rows={len(master):,} nullEndDate={int(master['securityEndDate'].isna().sum()):,}")

    members = universe_members(args.universe_dir, args.since)
    for name, ids in members.items():
        print(f"universe {name}: {len(ids):,} members since {args.since}")

    if args.ids_from_cache:
        wanted = set().union(*members.values()) if members else set()
        ids = [int(i) for i in master.index if int(i) in wanted]
        print(f"--ids-from-cache: querying {len(ids):,} of {len(master):,} ids")
    else:
        ids = [int(i) for i in master.index]

    consts = load_db_constants()
    conn = connect(consts, args.statement_timeout_ms)
    try:
        with conn.cursor() as cur:
            cur.execute("show transaction_read_only")
            ro = cur.fetchone()[0]
        if ro != "on":
            raise RuntimeError("session is not read-only")
        print(f"xfeed connected read-only (transaction_read_only={ro}); fetching {len(ids):,} ids")
        db = fetch_identity(conn, ids, args.batch_size, verbose=not args.quiet)
        status_names = fetch_status_names(conn)
    finally:
        conn.close()
    print(f"xfeed rows: {len(db):,}")

    refreshed_at = pd.Timestamp.now().floor("s")
    out, stats, detail = refresh_master(master, db, refreshed_at)

    print(
        f"endDateFilled={stats.end_date_filled} conflict={stats.end_date_conflict} "
        f"tickerChanged={stats.ticker_changed}(+{stats.ticker_filled} filled) "
        f"exchangeChanged={stats.exchange_changed} missingInDb={stats.n_missing_in_db}"
    )

    out_dir = args.out.parent
    report_path = args.report or (out_dir / "refresh_report.md")
    csv_paths = {name: out_dir / f"enddate_changes_{name}.csv" for name in members}

    if args.dry_run:
        print("[dry-run] nothing written")
        print(build_report(stats, detail, members, csv_paths, args.master, args.out, refreshed_at, CHECK_IDS, status_names))
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    out.to_parquet(args.out)
    print(f"wrote {args.out}")

    cols = [
        "symbolOld",
        "symbolNew",
        "companyName",
        "exchangeIdOld",
        "exchangeIdNew",
        "endDateOld",
        "endDateNew",
        "tradingItemStatusId",
        "tickerChanged",
        "exchangeChanged",
        "endDateConflict",
    ]
    for name, ids_set in members.items():
        idx = detail.index.intersection(pd.Index(sorted(ids_set), name=detail.index.name))
        sub = detail.loc[idx]
        sub = sub[sub["endDateChangedCum"]].sort_values("endDateNew", ascending=False)
        sub[cols].to_csv(csv_paths[name])
        print(f"wrote {csv_paths[name]} ({len(sub)} rows)")

    report_path.write_text(
        build_report(stats, detail, members, csv_paths, args.master, args.out, refreshed_at, CHECK_IDS, status_names)
    )
    print(f"wrote {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
