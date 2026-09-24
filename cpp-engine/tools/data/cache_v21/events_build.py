#!/home/jgo/usr/venv-3.11/bin/python
"""T6 (agent-events): regenerate the Phase 2 events file (contract v3) on the cache_v2.1 **raw** price basis.

`build_events.py` is not modified; this module only prepares its inputs, runs it, and post-processes:

  prepare-master  T1 master/current + instruments + lifecycle -> the indexed master build_events.load_master() reads.
                  securityEndDate = lifecycle.lastTradeDate for status=='confirmed' only (active/provisional: NaT),
                  the DB securityEndDate is kept as securityEndDateOriginal.
  prepare-close   loader export (v1 wide, raw close) -> the close panel build_events --close-cache reads, with the
                  post-lifecycle tail (date > lastTradeDate) set to NaN (= what publish flags qualityFlag 4).
  run             invoke build_events.py with those inputs (subprocess, log captured).
  finalize        cast ids to int64, add/fill lineage (rename/relist, high) succession rows, write corporate_actions.parquet.
  gates           events_gates.md: k identity, lead's independent v3 check, diff vs proto events, guard vs base_data,
                  BHI/CHFC/FI end dates, split_adj counter-example, lineage summary.

All DB access goes through build_events.connect() (read-only session). Nothing under /home/base or /home/jgo/61 is written.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
sys.path.insert(0, str(TOOLS))
import build_events as be  # noqa: E402  (import only; the file itself is never modified)

PY = "/home/jgo/usr/venv-3.11/bin/python"
SCRATCH = Path("/tmp/claude-1008/-home-jgo/3f2abe1d-a2a7-4017-9ac1-2c7cb961e311/scratchpad")
STAGE = SCRATCH / "stage_v21"
PROTO_EVENTS_DIR = Path("/home/jgo/61/data2/temp_stock/compare_v1_v2/events_v2_altaba")
PROTO_UNIVERSE_DIR = Path("/home/jgo/61/data2/temp_stock/cache_v2_cash_1d/TopUniverseV2_v1rule")
USD_CURRENCY = 160
MAX_RENAME_RETURN = 0.2   # same guard as build_events --max-rename-return
LINEAGE_RELATIONS = ("rename", "relist")

FINAL_SCHEMA = pa.schema([
    ("tradingItemId", pa.int64()), ("endDate", pa.timestamp("ns")), ("settlementDate", pa.timestamp("ns")),
    ("actionCode", pa.string()), ("settlementReturn", pa.float64()), ("suggestedReturn", pa.float64()),
    ("cashPerShare", pa.float64()), ("successorTradingItemId", pa.int64()), ("successorRatio", pa.float64()),
    ("acquirerName", pa.string()), ("source", pa.string()), ("confidence", pa.string()),
    ("actionText", pa.string()), ("matchMethod", pa.string()),
    ("endDateSource", pa.string()), ("endDateConflict", pa.bool_()),
    ("dbEndDateReason", pa.string()), ("endDateOriginal", pa.timestamp("ns")),
    ("endDateGapDays", pa.int32()), ("reviewFlag", pa.string()), ("endDateBasis", pa.string()),
    ("acquirerPriceDate", pa.timestamp("ns")),
    ("hasCashLeg", pa.bool_()), ("hasStockLeg", pa.bool_()), ("cacheUnitStatus", pa.string()),
    ("overrideSource", pa.string()),
    ("cashPerShareCache", pa.float64()), ("successorRatioCache", pa.float64()),
    ("cacheUnitFactorTarget", pa.float64()), ("cacheUnitFactorSuccessor", pa.float64()),
    ("targetCacheMark", pa.float64()), ("targetCacheMarkDate", pa.timestamp("ns")),
    ("cacheUnitFactorTargetDate", pa.timestamp("ns")),
    ("successorCacheClose", pa.float64()), ("successorCacheCloseDate", pa.timestamp("ns")),
])
assert [f.name for f in FINAL_SCHEMA] == be.EVENT_COLUMNS, "final schema must keep the v3 column order"


def log(*a):
    print(f"[{dt.datetime.now():%H:%M:%S}]", *a, file=sys.stderr, flush=True)


def sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(32 << 20), b""):
            h.update(block)
    return h.hexdigest()


def table(rows, header) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join("" if v is None else str(v) for v in row) + " |")
    return "\n".join(lines)


def fmt(v, digits=6):
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "NaN"
    if isinstance(v, (pd.Timestamp, np.datetime64)):
        return "NaT" if pd.isna(v) else str(pd.Timestamp(v).date())
    if isinstance(v, float):
        return f"{v:.{digits}g}"
    return str(v)


# ------------------------------------------------------------------------------------------------
# 1. master
# ------------------------------------------------------------------------------------------------
def currency_iso(codes, statement_timeout=60000) -> dict:
    """tiCurrencyId -> ISO code from xfl_target.ciqcurrency (tiny table, read-only). Falls back to {160: 'USD'}."""
    out = {USD_CURRENCY: "USD"}
    codes = sorted({int(c) for c in codes if pd.notna(c)})
    try:
        conn = be.connect(statement_timeout)
        try:
            frame = be.query(conn, f"SELECT currencyid, isocode FROM {be.SCHEMA_NAME}.ciqcurrency "
                                   f"WHERE currencyid = ANY(%s)", (codes,))
        finally:
            conn.close()
        out.update({int(r.currencyid): str(r.isocode) for r in frame.itertuples()})
    except Exception as exc:  # pragma: no cover - DB unavailable
        log(f"currency lookup failed ({exc}); ISOCode only for USD")
    return out


def prepare_master(t1: Path, out: Path) -> dict:
    cur = pd.read_parquet(t1 / "master" / "current.parquet")
    ins = pd.read_parquet(t1 / "master" / "instruments.parquet")
    lc = pd.read_parquet(t1 / "master" / "lifecycle.parquet")
    for frame, name in ((cur, "current"), (ins, "instruments"), (lc, "lifecycle")):
        if not frame["tradingItemId"].is_unique:
            raise RuntimeError(f"{name}: duplicate tradingItemId")
    if set(cur.tradingItemId) != set(lc.tradingItemId) or set(cur.tradingItemId) != set(ins.tradingItemId):
        raise RuntimeError("current / instruments / lifecycle id sets differ")
    m = cur.merge(ins[["tradingItemId", "securityId", "companyId", "companyName", "tiCurrencyId",
                       "inProtoV2"]], on="tradingItemId", how="left")
    lc_cols = ["tradingItemId", "firstTradeDate", "ciqLastCloseDate", "miLastCloseDate", "eventEndDate",
               "lastTradeDate", "lastTradeSource", "endReason", "status"]
    m = m.merge(lc[lc_cols].rename(columns={"status": "lifecycleStatus"}), on="tradingItemId", how="left")
    m["securityEndDateOriginal"] = pd.to_datetime(m["securityEndDate"])
    confirmed = m["lifecycleStatus"].eq("confirmed")
    m["securityEndDate"] = pd.to_datetime(m["lastTradeDate"]).where(confirmed)
    iso = currency_iso(m["tiCurrencyId"].dropna().unique())
    m["ISOCode"] = m["tiCurrencyId"].map(lambda c: iso.get(int(c)) if pd.notna(c) else None)
    m["currentPrimary"] = m["currentPrimary"].fillna(False).astype(bool)
    m = m.set_index(m["tradingItemId"].astype("int64")).drop(columns=["tradingItemId"])
    m.index.name = "tradingItemId"
    confirmed = m["lifecycleStatus"].eq("confirmed")   # re-derive on the new index (boolean alignment)
    out.parent.mkdir(parents=True, exist_ok=True)
    m.to_parquet(out)
    back = be.load_master(str(out))  # the exact reader build_events uses
    stats = {
        "rows": int(len(m)), "confirmed": int(confirmed.sum()),
        "status": {k: int(v) for k, v in m["lifecycleStatus"].value_counts().items()},
        "securityEndDate_set": int(m["securityEndDate"].notna().sum()),
        "securityEndDate_equals_confirmed": bool(m["securityEndDate"].notna().sum() == confirmed.sum()),
        "orig_enddate_present": int(m["securityEndDateOriginal"].notna().sum()),
        "confirmed_without_orig_enddate": int((confirmed & m["securityEndDateOriginal"].isna()).sum()),
        "orig_enddate_but_not_confirmed": int((~confirmed & m["securityEndDateOriginal"].notna()).sum()),
        "enddate_ne_orig_among_confirmed": int((confirmed & m["securityEndDateOriginal"].notna() &
                                                (m["securityEndDate"] != m["securityEndDateOriginal"])).sum()),
        "ISOCode": {k: int(v) for k, v in m["ISOCode"].fillna("(none)").value_counts().items()},
        "load_master_columns": list(back.columns), "load_master_rows": int(len(back)),
        "lifecycle_mtime": dt.datetime.fromtimestamp((t1 / "master" / "lifecycle.parquet").stat().st_mtime).isoformat(timespec="seconds"),
        "lifecycle_sha256": sha256(t1 / "master" / "lifecycle.parquet")[:12],
        "out": str(out), "sha256": sha256(out)[:12],
    }
    log(f"master: {json.dumps(stats, default=str)}")
    return stats


# ------------------------------------------------------------------------------------------------
# 2. close panel with the post-lifecycle tail removed
# ------------------------------------------------------------------------------------------------
def read_wide(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path)
    df.index = pd.DatetimeIndex(df.index)
    df.columns = [str(c) for c in df.columns]
    return df


def write_wide(df: pd.DataFrame, path: Path) -> None:
    """Same layout as loader.export_wide / v1: index -> '__index_level_0__' (timestamp[ns]), columns str(id)."""
    df = df.copy(deep=False)
    df.index = pd.DatetimeIndex(df.index.values)
    df.index.name = None
    tbl = pa.Table.from_pandas(df, preserve_index=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(tbl, path, compression="zstd", row_group_size=1024, use_dictionary=False)


def prepare_close(raw_close: Path, lifecycle: Path, out: Path, cut: bool = True) -> dict:
    df = read_wide(raw_close)
    lc = pd.read_parquet(lifecycle, columns=["tradingItemId", "lastTradeDate", "status"])
    lc["tradingItemId"] = lc["tradingItemId"].astype("int64")
    ids = np.asarray([int(c) for c in df.columns], dtype="int64")
    last = lc.set_index("tradingItemId")["lastTradeDate"].reindex(ids)
    arr = np.ascontiguousarray(df.to_numpy(dtype=np.float32))
    dates = df.index.values.astype("datetime64[ns]")
    stats = {"rows": int(arr.shape[0]), "cols": int(arr.shape[1]), "cut": bool(cut),
             "ids_without_lifecycle": int(last.isna().sum()),
             "finite_cells_before": int(np.isfinite(arr).sum())}
    if cut:
        ltd = last.values.astype("datetime64[ns]")
        post = dates[:, None] > ltd[None, :]          # NaT compares False -> never cut
        cut_cells = post & np.isfinite(arr)
        per_id = cut_cells.sum(axis=0)
        stats.update({
            "post_lifecycle_cells_finite": int(cut_cells.sum()),
            "ids_with_cut": int((per_id > 0).sum()),
            "max_cut_bars_id": int(ids[int(per_id.argmax())]) if per_id.max() > 0 else None,
            "max_cut_bars": int(per_id.max()),
        })
        arr[post] = np.nan
        top = np.argsort(-per_id)[:15]
        stats["top_cut"] = [(int(ids[j]), int(per_id[j]), str(pd.Timestamp(ltd[j]).date())) for j in top if per_id[j] > 0]
    stats["finite_cells_after"] = int(np.isfinite(arr).sum())
    write_wide(pd.DataFrame(arr, index=df.index, columns=df.columns, copy=False), out)
    stats["out"] = str(out)
    stats["sha256"] = sha256(out)[:12]
    stats["lifecycle_sha256"] = sha256(lifecycle)[:12]
    log(f"close: {json.dumps({k: v for k, v in stats.items() if k != 'top_cut'}, default=str)}")
    return stats


# ------------------------------------------------------------------------------------------------
# 3. run build_events.py
# ------------------------------------------------------------------------------------------------
def run_build_events(master: Path, close: Path, override: Path, out: Path, universe_dir: Path | None,
                     start: str, extra: list[str], log_path: Path) -> dict:
    cmd = [PY, str(TOOLS / "build_events.py"), "--master", str(master), "--out", str(out),
           "--override", str(override), "--close-cache", str(close), "--start", start,
           "--report", str(out.parent / "events_report.md")]
    if universe_dir is None:
        cmd.append("--skip-universe")
    else:
        cmd += ["--universe-dir", str(universe_dir)]
    cmd += extra
    env = dict(os.environ, OMP_NUM_THREADS="8", OPENBLAS_NUM_THREADS="8", MKL_NUM_THREADS="8",
               NUMEXPR_NUM_THREADS="8", POLARS_MAX_THREADS="8")
    out.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    log("run: " + " ".join(cmd))
    with open(log_path, "w") as fh:
        proc = subprocess.run(["/usr/bin/time", "-v"] + cmd, stdout=fh, stderr=subprocess.STDOUT, env=env,
                              cwd=str(TOOLS))
    seconds = time.time() - t0
    text = log_path.read_text(errors="replace")
    rss = next((line.split(":")[-1].strip() for line in text.splitlines() if "Maximum resident" in line), "?")
    info = {"cmd": " ".join(cmd), "exit": proc.returncode, "seconds": round(seconds, 1), "peak_rss_kb": rss,
            "log": str(log_path)}
    log(f"run: exit {proc.returncode} in {seconds:.0f}s, peak RSS {rss} kB")
    if proc.returncode != 0:
        raise RuntimeError(f"build_events failed (exit {proc.returncode}); see {log_path}")
    info["out_sha256"] = sha256(out)[:12]
    return info


# ------------------------------------------------------------------------------------------------
# 4. finalize: int64 ids + lineage succession rows
# ------------------------------------------------------------------------------------------------
def _to_int64_nullable(series: pd.Series) -> pd.Series:
    def conv(v):
        if v is None or v is pd.NA or (isinstance(v, float) and not np.isfinite(v)) or \
                (isinstance(v, str) and not v.strip()):
            return pd.NA
        if isinstance(v, (int, np.integer)):
            return int(v)
        if isinstance(v, (float, np.floating)):
            if abs(v - round(v)) > 1e-9:
                raise ValueError(f"non-integer id {v!r}")
            return int(round(v))
        return int(str(v).strip())
    return series.map(conv).astype("Int64")


def _lineage_row(tid: int, new_id: int, ratio_in: float, pair, ev_row, closes: be.CloseCache, lc: pd.DataFrame,
                 symbols: dict, as_traded_t: dict, as_traded_s: dict, start: pd.Timestamp) -> tuple[dict | None, str]:
    """Build (or fill) one succession row exactly on build_events' measurement path.

    Returns (row dict, action). `ev_row` is the existing row (Series) when filling, else None."""
    old = lc.loc[tid]
    if ev_row is None:
        end = pd.Timestamp(old["lastTradeDate"]).normalize()
        end_source, end_basis, end_orig, gap, flag, settle_date = "lifecycle", "original", end, 0, "", end
        if pd.notna(pair.effectiveDate):
            settle_date = pd.Timestamp(pair.effectiveDate).normalize()
    else:
        end = pd.Timestamp(ev_row["endDate"]).normalize()
        end_source, end_basis = str(ev_row["endDateSource"]), str(ev_row["endDateBasis"])
        end_orig, gap, flag = ev_row["endDateOriginal"], int(ev_row["endDateGapDays"]), str(ev_row["reviewFlag"])
        settle_date = ev_row["settlementDate"]
    if pd.isna(end) or end < start:
        return None, "skipped_end_before_start"
    stid = str(tid)
    if not closes.has(stid):
        return None, "skipped_target_not_in_panel"
    bar, deferred = closes.settlement_bar(stid, end)
    if bar is pd.NaT:
        return None, "skipped_no_settlement_bar"
    mark, mark_date = closes.last_valid_before(stid, bar)
    cache_kt, kt_date = closes.last_on_or_before(stid, end)
    succ_cache = closes.value_on(str(new_id), bar) if closes.has(str(new_id)) else np.nan
    if not (np.isfinite(succ_cache) and succ_cache > 0):
        # "새 id가 그 뒤 거래되는 경우": the successor must be quoted after the old line stops
        price_after, when_after = closes.first_on_or_after(str(new_id), bar, max_days=60) if closes.has(str(new_id)) \
            else (np.nan, pd.NaT)
        if not np.isfinite(price_after):
            return None, "skipped_successor_not_quoted_after_end"
    ratio = float(ratio_in) if pd.notna(ratio_in) else 1.0
    assumed = not pd.notna(ratio_in)
    at_t = as_traded_t.get((tid, kt_date), np.nan) if pd.notna(kt_date) else np.nan
    at_s = as_traded_s.get((new_id, bar), np.nan)
    res = be.resolve_cache_units(has_cash=False, has_stock=True, cash=np.nan, ratio=ratio, has_successor=True,
                                 as_traded_target=at_t, cache_at_kt=cache_kt, cache_mark=mark,
                                 as_traded_successor=at_s, cache_successor=succ_cache)
    k_t = res["cacheUnitFactorTarget"]
    last_close = mark * k_t if np.isfinite(k_t) else np.nan
    r = (ratio * at_s) / last_close - 1.0 if (np.isfinite(at_s) and np.isfinite(last_close) and last_close > 0) else np.nan
    status = res["cacheUnitStatus"]
    ratio_cache = res["successorRatioCache"]
    reject = assumed and np.isfinite(r) and abs(r) > MAX_RENAME_RETURN
    if reject:
        status, ratio_cache = "rejected:rename_not_1to1", np.nan
        r_out = np.nan
    else:
        r_out = r
    convertible = np.isfinite(ratio_cache)
    row = {
        "tradingItemId": int(tid), "endDate": end, "settlementDate": pd.Timestamp(settle_date).normalize()
        if pd.notna(settle_date) else end,
        "actionCode": "N/C", "settlementReturn": r_out, "suggestedReturn": np.nan,
        "cashPerShare": 0.0, "successorTradingItemId": int(new_id), "successorRatio": ratio,
        "acquirerName": symbols.get(int(new_id)),
        "source": "lineage_assumed_ratio_1" if assumed else "lineage",
        "confidence": f"lineage_{pair.confidence}",
        "actionText": (f"lineage {pair.relation} {tid}->{new_id} ({pair.evidence})")[:200],
        "matchMethod": "lineage", "endDateSource": end_source, "endDateConflict": False, "dbEndDateReason": "",
        "endDateOriginal": end_orig, "endDateGapDays": gap, "reviewFlag": flag, "endDateBasis": end_basis,
        "acquirerPriceDate": bar if np.isfinite(at_s) else pd.NaT,
        "hasCashLeg": False, "hasStockLeg": True, "cacheUnitStatus": status, "overrideSource": "",
        "cashPerShareCache": np.nan, "successorRatioCache": ratio_cache,
        "cacheUnitFactorTarget": k_t, "cacheUnitFactorSuccessor": res["cacheUnitFactorSuccessor"],
        "targetCacheMark": mark if convertible else np.nan, "targetCacheMarkDate": mark_date if convertible else pd.NaT,
        "cacheUnitFactorTargetDate": kt_date if np.isfinite(k_t) else pd.NaT,
        "successorCacheClose": succ_cache if convertible else np.nan,
        "successorCacheCloseDate": bar if convertible else pd.NaT,
    }
    return row, ("filled" if ev_row is not None else "added")


def finalize(built: Path, lineage: Path, lifecycle: Path, current: Path, close: Path, out: Path, lineage_csv: Path,
             start: str, mode: str = "add-and-fill", statement_timeout: int = 300000) -> dict:
    ev = pq.read_table(built).to_pandas()
    assert list(ev.columns) == be.EVENT_COLUMNS, "built file is not the v3 column set"
    ev["tradingItemId"] = ev["tradingItemId"].astype(str).str.strip().astype("int64")
    ev["successorTradingItemId"] = _to_int64_nullable(ev["successorTradingItemId"])
    if not ev["tradingItemId"].is_unique:
        raise RuntimeError("duplicate tradingItemId in built events")
    stats = {"built_rows": int(len(ev)), "mode": mode}

    lin = pd.read_parquet(lineage)
    lc = pd.read_parquet(lifecycle).set_index("tradingItemId")
    cur = pd.read_parquet(current)
    symbols = {int(t): (None if pd.isna(s) else str(s)) for t, s in zip(cur.tradingItemId, cur.symbol)}
    start_ts = pd.Timestamp(start)
    cand = lin[lin["relation"].isin(LINEAGE_RELATIONS) & lin["confidence"].eq("high")].copy()
    cand["oldId"] = cand["oldId"].astype("int64")
    cand["newId"] = cand["newId"].astype("int64")
    cand = cand[cand["oldId"] != cand["newId"]]
    cand = cand[cand["oldId"].isin(lc.index)]
    cand["oldStatus"] = cand["oldId"].map(lc["status"])
    cand["oldLastTradeDate"] = cand["oldId"].map(lc["lastTradeDate"])
    cand = cand[cand["oldStatus"].eq("confirmed")]
    # one successor per old id: keep the pair whose effectiveDate is closest after the old line's last trade
    cand["_gap"] = (pd.to_datetime(cand["effectiveDate"]) - pd.to_datetime(cand["oldLastTradeDate"])).dt.days.abs()
    cand = cand.sort_values(["oldId", "_gap"]).drop_duplicates("oldId", keep="first")
    stats["lineage_pairs_high_same_line_confirmed"] = int(len(cand))

    existing = ev.set_index("tradingItemId")
    closes = be.CloseCache(str(close))
    records = []
    plan = []   # (tid, new_id, ratio, pair, ev_row or None)
    for pair in cand.itertuples():
        tid, new_id = int(pair.oldId), int(pair.newId)
        if tid in existing.index:
            row = existing.loc[tid]
            if pd.notna(row["successorTradingItemId"]):
                action = "kept_existing_successor" if int(row["successorTradingItemId"]) == new_id \
                    else "kept_existing_other_successor"
                records.append(dict(oldId=tid, newId=new_id, relation=pair.relation, ratio=pair.ratio,
                                    action=action, existingSuccessor=int(row["successorTradingItemId"]),
                                    existingStatus=row["cacheUnitStatus"], existingSource=row["source"]))
                continue
            has_terms = pd.notna(row["cashPerShare"]) or pd.notna(row["successorRatio"]) or bool(row["hasStockLeg"]) \
                or bool(row["hasCashLeg"])
            if has_terms or mode == "add-only":
                records.append(dict(oldId=tid, newId=new_id, relation=pair.relation, ratio=pair.ratio,
                                    action="skipped_has_terms" if has_terms else "skipped_add_only_mode",
                                    existingSuccessor=None, existingStatus=row["cacheUnitStatus"],
                                    existingSource=row["source"]))
                continue
            plan.append((tid, new_id, pair.ratio, pair, row))
        else:
            plan.append((tid, new_id, pair.ratio, pair, None))
    # as-traded prints for the k anchors (same fetch as build_events: ciqpriceequity priceclose x adjustmentfactor)
    req_t, req_s, meta = [], [], []
    for tid, new_id, ratio, pair, row in plan:
        end = pd.Timestamp(row["endDate"]).normalize() if row is not None else pd.Timestamp(lc.at[tid, "lastTradeDate"]).normalize()
        if pd.isna(end) or not closes.has(str(tid)):
            continue
        bar, _ = closes.settlement_bar(str(tid), end)
        _, kt_date = closes.last_on_or_before(str(tid), end)
        if pd.notna(kt_date):
            req_t.append((tid, kt_date.date(), kt_date.date()))
        if bar is not pd.NaT:
            req_s.append((new_id, bar.date(), bar.date()))
    as_traded_t, as_traded_s = {}, {}
    if req_t or req_s:
        conn = be.connect(statement_timeout)
        try:
            for tid, series in be.fetch_as_traded(conn, req_t).items():
                for when, price in series:
                    as_traded_t[(int(tid), pd.Timestamp(when))] = price
            for sid, series in be.fetch_as_traded(conn, req_s).items():
                for when, price in series:
                    as_traded_s[(int(sid), pd.Timestamp(when))] = price
        finally:
            conn.close()
    new_rows, filled_ids = [], []
    for tid, new_id, ratio, pair, row in plan:
        built_row, action = _lineage_row(tid, new_id, ratio, pair, row, closes, lc, symbols, as_traded_t, as_traded_s,
                                         start_ts)
        rec = dict(oldId=tid, newId=new_id, relation=pair.relation, ratio=pair.ratio, action=action,
                   existingSuccessor=None, existingStatus=None if row is None else row["cacheUnitStatus"],
                   existingSource=None if row is None else row["source"])
        if built_row is not None:
            rec.update(endDate=built_row["endDate"], successorRatio=built_row["successorRatio"],
                       successorRatioCache=built_row["successorRatioCache"], cacheUnitStatus=built_row["cacheUnitStatus"],
                       settlementReturn=built_row["settlementReturn"], source=built_row["source"],
                       k_t=built_row["cacheUnitFactorTarget"], k_s=built_row["cacheUnitFactorSuccessor"])
            if action == "added":
                new_rows.append(built_row)
            else:
                filled_ids.append(tid)
                for k, v in built_row.items():
                    ev.loc[ev["tradingItemId"] == tid, k] = v if not isinstance(v, (pd.Timestamp, type(pd.NaT))) \
                        else pd.Timestamp(v)
        records.append(rec)
    if new_rows:
        add = pd.DataFrame(new_rows)[be.EVENT_COLUMNS]
        ev = pd.concat([ev, add], ignore_index=True)
    ev["successorTradingItemId"] = _to_int64_nullable(ev["successorTradingItemId"])
    if not ev["tradingItemId"].is_unique:
        raise RuntimeError("duplicate tradingItemId after lineage rows")
    ev = ev.sort_values(["endDate", "tradingItemId"]).reset_index(drop=True)
    rec_df = pd.DataFrame(records)
    lineage_csv.parent.mkdir(parents=True, exist_ok=True)
    rec_df.to_csv(lineage_csv, index=False)
    stats["lineage_actions"] = {k: int(v) for k, v in rec_df["action"].value_counts().items()} if len(rec_df) else {}
    stats["lineage_added"] = len(new_rows)
    stats["lineage_filled"] = len(filled_ids)
    if len(rec_df) and "source" in rec_df:
        stats["lineage_source"] = {k: int(v) for k, v in rec_df["source"].dropna().value_counts().items()}
        stats["lineage_status"] = {k: int(v) for k, v in rec_df["cacheUnitStatus"].dropna().value_counts().items()}

    # write with the fixed schema (ids int64)
    for col in ("endDate", "settlementDate", "endDateOriginal", "acquirerPriceDate", "targetCacheMarkDate",
                "cacheUnitFactorTargetDate", "successorCacheCloseDate"):
        ev[col] = pd.to_datetime(ev[col]).astype("datetime64[ns]")
    ev["endDateGapDays"] = pd.to_numeric(ev["endDateGapDays"]).fillna(0).astype("int32")
    for col in ("endDateConflict", "hasCashLeg", "hasStockLeg"):
        ev[col] = ev[col].astype(bool)
    for col in ("actionCode", "acquirerName", "source", "confidence", "actionText", "matchMethod", "endDateSource",
                "dbEndDateReason", "reviewFlag", "endDateBasis", "cacheUnitStatus", "overrideSource"):
        ev[col] = ev[col].astype("object").where(ev[col].notna(), None)
    tbl = pa.Table.from_pandas(ev[be.EVENT_COLUMNS], schema=FINAL_SCHEMA, preserve_index=False)
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(tbl, out, compression="zstd")
    stats.update(final_rows=int(len(ev)), out=str(out), sha256=sha256(out)[:12],
                 source_counts={k: int(v) for k, v in ev["source"].value_counts().items()},
                 status_counts={k: int(v) for k, v in ev["cacheUnitStatus"].value_counts().items()})
    log(f"finalize: {json.dumps({k: v for k, v in stats.items()}, default=str)}")
    return stats


# ------------------------------------------------------------------------------------------------
# 4b. rc2 / R3: apply the rename reclassification (confirmed exchange ratios) to the rejected 1:1 rows
# ------------------------------------------------------------------------------------------------
RECLASS_STATUSES = ("rejected:rename_not_1to1",)
SUCCESSOR_AFTER_BAR_DAYS = 7      # settlementReturn of the events-row legs: successor priced at its first DB close on/after the settlement bar


def apply_reclassification(events: Path, reclass_csv: Path, lifecycle: Path, current: Path, close: Path, out: Path,
                           start: str = "2000-01-01", statement_timeout: int = 300000, include_estimates: bool = True) -> dict:
    """Rebuild every `rejected:rename_not_1to1` row that has a confirmed successor ratio in `reclass_csv`
    (master_lineage.py output, columns tradingItemId, successorTradingItemId, successorRatio, ratioSource, linkClass, confidence,
    cashPerShare, effectiveDate, evidence) on the same measurement path as the lineage rows (`_lineage_row`): k anchors from
    ciqpriceequity as-traded prints, cache-unit columns, settlementReturn with the given ratio (no 1:1 assumption -> no
    rename guard).  A merger cash leg (cashPerShare) is added to the row (hasCashLeg, cashPerShareCache = cash x k_t).
    Rows whose ratio source is `manual_public_terms` are estimates ([추정]); `include_estimates=False` leaves them rejected.
    Nothing else in the file changes; the output keeps the v3 column order and int64 ids."""
    ev = pq.read_table(events).to_pandas()
    assert list(ev.columns)[:len(be.EVENT_COLUMNS)] == be.EVENT_COLUMNS, "events file is not the v3 column set"
    ev["successorTradingItemId"] = _to_int64_nullable(ev["successorTradingItemId"])
    rc = pd.read_csv(reclass_csv)
    rc["effectiveDate"] = pd.to_datetime(rc["effectiveDate"])
    if not include_estimates:
        rc = rc[rc["ratioSource"] != "manual_public_terms"]
    lc = pd.read_parquet(lifecycle).set_index("tradingItemId")
    cur = pd.read_parquet(current)
    symbols = {int(t): (None if pd.isna(s) else str(s)) for t, s in zip(cur.tradingItemId, cur.symbol)}
    closes = be.CloseCache(str(close))
    if "settlementReturnScope" not in ev.columns:
        ev["settlementReturnScope"] = "full"          # rc1 rows: their settlementReturn covers the whole recorded consideration
    existing = ev.set_index("tradingItemId")
    plan, records = [], []
    for r in rc.itertuples():
        tid, new_id = int(r.tradingItemId), int(r.successorTradingItemId)
        if tid not in existing.index:
            records.append(dict(tradingItemId=tid, successor=new_id, action="skipped_no_event_row"))
            continue
        row = existing.loc[tid]
        if str(row["cacheUnitStatus"]) not in RECLASS_STATUSES:
            records.append(dict(tradingItemId=tid, successor=new_id, action=f"skipped_status_{row['cacheUnitStatus']}"))
            continue
        if pd.notna(row["successorTradingItemId"]) and int(row["successorTradingItemId"]) != new_id:
            records.append(dict(tradingItemId=tid, successor=new_id, action="skipped_other_successor", existing=int(row["successorTradingItemId"])))
            continue
        plan.append((tid, new_id, float(r.successorRatio), r, row))
    req_t, req_s = [], []
    for tid, new_id, ratio, r, row in plan:
        end = pd.Timestamp(row["endDate"]).normalize()
        ltd = lc.lastTradeDate.get(tid, pd.NaT)
        if pd.notna(ltd) and pd.Timestamp(ltd) < end:
            end = pd.Timestamp(ltd).normalize()
        if not closes.has(str(tid)):
            continue
        bar, _ = closes.settlement_bar(str(tid), end)
        _, kt_date = closes.last_on_or_before(str(tid), end)
        if pd.notna(kt_date):
            req_t.append((tid, kt_date.date(), kt_date.date()))
        if bar is not pd.NaT:
            req_s.append((new_id, bar.date(), (bar + pd.Timedelta(days=SUCCESSOR_AFTER_BAR_DAYS)).date()))
    as_traded_t, as_traded_s, succ_after = {}, {}, {}
    if req_t or req_s:
        conn = be.connect(statement_timeout)
        try:
            for tid, series in be.fetch_as_traded(conn, req_t).items():
                for when, price in series:
                    as_traded_t[(int(tid), pd.Timestamp(when))] = price
            for sid, series in be.fetch_as_traded(conn, req_s).items():
                for when, price in series:
                    as_traded_s[(int(sid), pd.Timestamp(when))] = price
                    succ_after.setdefault(int(sid), []).append((pd.Timestamp(when), price))
        finally:
            conn.close()
    start_ts = pd.Timestamp(start)
    n_applied = 0
    n_capped = 0
    scope_counts = {}
    for tid, new_id, ratio, r, row in plan:
        pair = pd.Series({"effectiveDate": r.effectiveDate, "confidence": f"{r.confidence}:{r.ratioSource}", "relation": r.linkClass,
                          "evidence": str(r.evidence)[:160]})
        # settle at the old line's last genuine close: an endDate after lifecycle.lastTradeDate (T6 enddate_gap) would mark the
        # position weeks before the settlement bar and price the successor after its stitched tail (P12: the dates are kept apart
        # in master/lifecycle_dates.parquet; the engine's endDate is the last trade date)
        ltd = lc.lastTradeDate.get(tid, pd.NaT)
        if pd.notna(ltd) and pd.Timestamp(ltd) < pd.Timestamp(row["endDate"]):
            row = row.copy()
            row["endDate"] = pd.Timestamp(ltd).normalize()
            row["endDateSource"] = str(row["endDateSource"]) + "+R3_capped_lastTradeDate"
            row["reviewFlag"] = (str(row["reviewFlag"]) + ";R3_capped_lastTradeDate").strip(";")
            n_capped += 1
        built, action = _lineage_row(tid, new_id, ratio, pair, row, closes, lc, symbols, as_traded_t, as_traded_s, start_ts)
        rec = dict(tradingItemId=tid, successor=new_id, ratio=ratio, ratioSource=r.ratioSource, linkClass=r.linkClass, action=action,
                   previousStatus=row["cacheUnitStatus"])
        if built is None:
            records.append(rec)
            continue
        built["actionCode"] = row["actionCode"] if isinstance(row["actionCode"], str) and row["actionCode"] else "N/C"
        built["source"] = "reclass_" + str(r.ratioSource)
        built["overrideSource"] = "R3_reclass"
        # F1: every leg of the consideration is either carried by this row (cash, successor stock) or named in reviewFlag
        # (extra stock legs = ledger spin_off rows on the settlement bar); a row with an unpriced leg is flagged partial_terms
        # and carries no fallback settlementReturn
        flag = str(getattr(r, "reviewFlag", "") or "")
        prev_flag = str(row["reviewFlag"]) if isinstance(row["reviewFlag"], str) and row["reviewFlag"] else ""
        built["reviewFlag"] = ";".join(x for x in (prev_flag, flag) if x)
        partial = flag.startswith("partial_terms")
        built["matchMethod"] = str(row["matchMethod"]) if isinstance(row["matchMethod"], str) and row["matchMethod"] != "none" else "lineage"
        built["actionText"] = (f"[R3 {r.linkClass}/{r.ratioSource}] " + str(r.evidence))[:200]
        k_t = built["cacheUnitFactorTarget"]
        cash = float(r.cashPerShare) if pd.notna(r.cashPerShare) else np.nan
        if np.isfinite(cash) and cash > 0:
            built["cashPerShare"] = cash
            built["hasCashLeg"] = True
            mark = built["targetCacheMark"]
            succ = built["successorCacheClose"]
            rcache = built["successorRatioCache"]
            # cache-unit cash only when the row is convertible (successor priced on the settlement bar), like _lineage_row's
            # stock leg: a stock leg without ratioCache must not carry cashCache (lead invariant 1)
            built["cashPerShareCache"] = cash * k_t if (np.isfinite(k_t) and np.isfinite(rcache)) else np.nan
            if np.isfinite(mark) and mark > 0 and np.isfinite(succ) and np.isfinite(rcache) and np.isfinite(built["cashPerShareCache"]):
                built["settlementReturn"] = (built["cashPerShareCache"] + rcache * succ) / mark - 1.0
        # settlementReturn = return of the legs THIS row carries (cash + successor), priced from DB as-traded closes like rc1:
        # old mark = as-traded close on the k anchor date, successor = first DB close on/after the settlement bar (<= 7 days), so a
        # successor missing from the engine panel still gets a priced fallback instead of default_delisting_return.  Extra stock legs
        # (children) are ledger spin_off rows keyed on the old id; settlementReturnScope says the value is not the full consideration.
        end_used = pd.Timestamp(row["endDate"]).normalize()
        bar_used, _ = closes.settlement_bar(str(tid), end_used)
        _, kt_used = closes.last_on_or_before(str(tid), end_used)
        at_t_used = as_traded_t.get((tid, kt_used), np.nan) if pd.notna(kt_used) else np.nan
        succ_px = [(w, p_) for w, p_ in succ_after.get(new_id, []) if bar_used is not pd.NaT and w >= bar_used and np.isfinite(p_)]
        succ_at, succ_when = (succ_px[0][1], succ_px[0][0]) if succ_px else (np.nan, pd.NaT)
        cash_legs = float(r.cashPerShare) if pd.notna(r.cashPerShare) and float(r.cashPerShare) > 0 else 0.0
        if np.isfinite(at_t_used) and at_t_used > 0 and np.isfinite(succ_at):
            built["settlementReturn"] = (cash_legs + ratio * succ_at) / at_t_used - 1.0
            if pd.notna(succ_when) and bar_used is not pd.NaT and succ_when != bar_used:
                built["reviewFlag"] = built["reviewFlag"] + f";successor_priced_{succ_when.date()}_after_bar"
        else:
            built["settlementReturn"] = np.nan
        if "stock=" in flag:
            built["reviewFlag"] = built["reviewFlag"].replace("multi_leg:", "multi_leg_split_path(child legs in ledger):", 1)
            scope = "events_legs_only"
        else:
            scope = "full"
        if partial:
            built["settlementReturn"] = np.nan
            scope = "partial_terms"
        built["settlementReturnScope"] = scope
        scope_counts[scope] = scope_counts.get(scope, 0) + 1
        for k, v in built.items():
            ev.loc[ev["tradingItemId"] == tid, k] = v if not isinstance(v, (pd.Timestamp, type(pd.NaT))) else pd.Timestamp(v)
        rec.update(newStatus=built["cacheUnitStatus"], settlementReturn=built["settlementReturn"], successorRatioCache=built["successorRatioCache"],
                   k_t=built["cacheUnitFactorTarget"], k_s=built["cacheUnitFactorSuccessor"], cashPerShare=built["cashPerShare"],
                   cashPerShareCache=built["cashPerShareCache"], reviewFlag=built["reviewFlag"], partial=partial, scope=built["settlementReturnScope"])
        records.append(rec)
        n_applied += 1
    ev["successorTradingItemId"] = _to_int64_nullable(ev["successorTradingItemId"])
    ev = ev.sort_values(["endDate", "tradingItemId"]).reset_index(drop=True)
    for col in ("endDate", "settlementDate", "endDateOriginal", "acquirerPriceDate", "targetCacheMarkDate",
                "cacheUnitFactorTargetDate", "successorCacheCloseDate"):
        ev[col] = pd.to_datetime(ev[col]).astype("datetime64[ns]")
    ev["endDateGapDays"] = pd.to_numeric(ev["endDateGapDays"]).fillna(0).astype("int32")
    for col in ("endDateConflict", "hasCashLeg", "hasStockLeg"):
        ev[col] = ev[col].astype(bool)
    for col in ("actionCode", "acquirerName", "source", "confidence", "actionText", "matchMethod", "endDateSource",
                "dbEndDateReason", "reviewFlag", "endDateBasis", "cacheUnitStatus", "overrideSource"):
        ev[col] = ev[col].astype("object").where(ev[col].notna(), None)
    if "settlementReturnScope" not in ev.columns:
        ev["settlementReturnScope"] = "full"
    ev["settlementReturnScope"] = ev["settlementReturnScope"].fillna("full").astype(str)
    tbl = pa.Table.from_pandas(ev[be.EVENT_COLUMNS + ["settlementReturnScope"]],
                               schema=pa.schema(list(FINAL_SCHEMA) + [pa.field("settlementReturnScope", pa.string())]), preserve_index=False)
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(tbl, out, compression="zstd")
    rec_df = pd.DataFrame(records)
    rec_df.to_csv(out.with_suffix(".reclass_log.csv"), index=False)
    stats = {"reclass_rows": int(len(rc)), "applied": n_applied, "endDate_capped_to_lastTradeDate": n_capped,
             "multi_leg_rows": int(rec_df["reviewFlag"].fillna("").str.contains("multi_leg").sum()) if "reviewFlag" in rec_df else 0,
             "partial_terms_rows": int(rec_df["partial"].fillna(False).sum()) if "partial" in rec_df else 0, "settlementReturnScope": scope_counts, "actions": {k: int(v) for k, v in rec_df["action"].value_counts().items()} if len(rec_df) else {},
             "new_status": {k: int(v) for k, v in rec_df["newStatus"].dropna().value_counts().items()} if "newStatus" in rec_df else {},
             "remaining_rejected_rename": int((ev["cacheUnitStatus"] == "rejected:rename_not_1to1").sum()),
             "status_counts": {k: int(v) for k, v in ev["cacheUnitStatus"].value_counts().items()}, "out": str(out), "sha256": sha256(out)[:12]}
    log(f"reclass: {json.dumps({k: v for k, v in stats.items() if k != 'status_counts'}, default=str)}")
    return stats


# ------------------------------------------------------------------------------------------------
# 5. gates
# ------------------------------------------------------------------------------------------------
def settlement_bars(close: pd.DataFrame, ids, ends) -> list:
    """First bar after end with no valid close (the engine rule) on a wide panel; NaT if never quiet."""
    dates = close.index.values
    out = []
    for tid, end in zip(ids, ends):
        col = str(int(tid))
        if col not in close.columns or pd.isna(end):
            out.append(pd.NaT)
            continue
        s = close[col].to_numpy(dtype=np.float64)
        start = int(np.searchsorted(dates, np.datetime64(pd.Timestamp(end).normalize()), "right"))
        quiet = np.flatnonzero(~(np.isfinite(s[start:]) & (s[start:] > 0)))
        out.append(pd.Timestamp(dates[start + quiet[0]]) if quiet.size and start < len(dates) else pd.NaT)
    return out


def gate_k_identity(ev: pd.DataFrame) -> tuple[list, pd.DataFrame]:
    rows, bad = [], []
    for name, cache_col, raw_col in (("cashPerShareCache/cashPerShare − 1", "cashPerShareCache", "cashPerShare"),
                                     ("successorRatioCache/successorRatio − 1", "successorRatioCache", "successorRatio")):
        m = ev[cache_col].notna() & ev[raw_col].notna() & (ev[raw_col] != 0)
        dev = (ev.loc[m, cache_col] / ev.loc[m, raw_col] - 1.0).abs()
        rows.append([name, int(m.sum()), fmt(float(dev.max()) if len(dev) else 0.0, 3),
                     int((dev > 1e-6).sum()), int((dev > 1e-4).sum())])
        b = ev.loc[m].assign(dev=dev)[dev > 1e-6]
        if len(b):
            bad.append(b.assign(which=name))
    for name, col in (("cacheUnitFactorTarget − 1", "cacheUnitFactorTarget"),
                      ("cacheUnitFactorSuccessor − 1", "cacheUnitFactorSuccessor")):
        m = ev[col].notna()
        dev = (ev.loc[m, col] - 1.0).abs()
        rows.append([name, int(m.sum()), fmt(float(dev.max()) if len(dev) else 0.0, 3),
                     int((dev > 1e-6).sum()), int((dev > 1e-4).sum())])
    bad_df = pd.concat(bad) if bad else pd.DataFrame()
    return rows, bad_df


def run_lead_check(script: Path, events: Path, close: Path) -> tuple[int, str]:
    proc = subprocess.run([PY, str(script), str(events), str(close)], capture_output=True, text=True)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def read_base_close(base_data: Path, ids: set) -> pd.DataFrame:
    """(tradingItemId, date, close) rows of T2 base_data for the given ids (row-group streaming)."""
    pf = pq.ParquetFile(base_data)
    ids_arr = np.fromiter(ids, dtype="int64")
    frames = []
    for rg in range(pf.num_row_groups):
        t = pf.read_row_group(rg, columns=["tradingItemId", "date", "close"])
        keep = np.isin(t.column("tradingItemId").to_numpy(), ids_arr)
        if keep.any():
            frames.append(t.filter(pa.array(keep)).to_pandas())
    if not frames:
        return pd.DataFrame(columns=["tradingItemId", "date", "close"])
    out = pd.concat(frames, ignore_index=True)
    out["date"] = pd.to_datetime(out["date"])
    return out


def _src_kind(series: pd.Series) -> pd.Series:
    return series.astype(str).str.replace(r"^(db|local)\+capped_(recorddate_match|estimate)$", r"\1+cap", regex=True)


def compare_proto(ev: pd.DataFrame, proto: pd.DataFrame, lc: pd.DataFrame) -> tuple[list, pd.DataFrame, dict]:
    """Common ids compared on every engine-relevant column; end-date/successor/R differences classified by cause."""
    p = proto.copy()
    p["tradingItemId"] = p["tradingItemId"].astype(str).str.strip().astype("int64")
    p["successorTradingItemId"] = _to_int64_nullable(p["successorTradingItemId"])
    a = ev.set_index("tradingItemId")
    b = p.set_index("tradingItemId")
    common = a.index.intersection(b.index)
    only_new = a.index.difference(b.index)
    only_old = b.index.difference(a.index)
    aa, bb = a.loc[common], b.loc[common]
    lcx = lc.set_index("tradingItemId")
    ltd = lcx["lastTradeDate"].reindex(common)
    diffs = {}
    d_end = aa["endDate"] != bb["endDate"]
    diffs["endDate"] = d_end
    s1, s2 = aa["successorTradingItemId"], bb["successorTradingItemId"]
    d_succ = ~((s1.isna() & s2.isna()) | (s1.notna() & s2.notna() & (s1.fillna(-1).astype("int64") == s2.fillna(-1).astype("int64"))))
    diffs["successorTradingItemId"] = d_succ
    for col in ("successorRatio", "cashPerShare", "settlementReturn", "successorRatioCache", "cashPerShareCache"):
        x, y = aa[col], bb[col]
        diffs[col] = ~((x.isna() & y.isna()) | (np.isclose(x.fillna(0), y.fillna(0), rtol=1e-9, atol=1e-12) & x.notna() & y.notna()))
    d_status = aa["cacheUnitStatus"] != bb["cacheUnitStatus"]
    diffs["cacheUnitStatus"] = d_status
    same_ratio = ~diffs["successorRatio"]
    same_cash = ~diffs["cashPerShare"]
    rows = [["공통 종목", len(common), ""],
            ["v2.1에만 있는 종목(2009+ 종료, proto 없음)", int((a.loc[only_new, "endDate"] >= "2009-01-01").sum()), ""],
            ["v2.1에만 있는 종목(2000~2008 종료; proto는 --start 2009)", int((a.loc[only_new, "endDate"] < "2009-01-01").sum()), ""],
            ["proto에만 있는 종목", len(only_old), ""]]
    for col, m in diffs.items():
        rows.append([f"{col} 차이", int(m.sum()), f"{m.sum() / max(len(common), 1):.2%}"])
    rows.append(["  successorRatioCache 차이 중 원 successorRatio 동일(= 앵커 기준 변경, 기대된 차이)",
                 int((diffs["successorRatioCache"] & same_ratio).sum()), ""])
    rows.append(["  cashPerShareCache 차이 중 원 cashPerShare 동일(= 앵커 기준 변경)",
                 int((diffs["cashPerShareCache"] & same_cash).sum()), ""])
    # --- end-date cause -------------------------------------------------------------------
    k21, kp = _src_kind(aa["endDateSource"]), _src_kind(bb["endDateSource"])
    dd = (aa["endDate"] - bb["endDate"]).dt.days
    eq_ltd = aa["endDate"] == ltd
    cause = pd.Series("", index=common, dtype=object)
    cause[d_end & k21.eq("override")] = "override"
    cause[d_end & (cause == "") & k21.str.endswith("+cap") & ~kp.str.endswith("+cap") & (aa["endDate"] <= ltd)] = \
        "tail_cut_cap: 절단 패널의 마지막 종가(≤lastTradeDate)로 캡, proto는 꼬리 호가로 캡 없음"
    cause[d_end & (cause == "") & k21.eq("local") & eq_ltd] = "lifecycle_local: DB 종료일 불신(security-level) → lastTradeDate"
    cause[d_end & (cause == "") & ~k21.str.endswith("+cap") & kp.str.endswith("+cap")] = "proto_capped_v21_not: proto 패널 마지막 호가가 더 늦어 캡 근거(≤5d) 성립"
    cause[d_end & (cause == "") & eq_ltd] = "equals_lastTradeDate(other)"
    cause[d_end & (cause == "")] = "other"
    end_causes = [[k, int(v), int(dd[cause == k].median()), int(dd[cause == k].min()), int(dd[cause == k].max())]
                  for k, v in cause[d_end].value_counts().items()]
    # --- successor cause ------------------------------------------------------------------
    scause = pd.Series("", index=common, dtype=object)
    scause[d_succ & s1.notna() & s2.isna() & aa["matchMethod"].eq("lineage")] = "lineage_fill"
    scause[d_succ & s1.notna() & s2.isna() & (scause == "")] = "gained_successor(resolver: 정산 bar 가격 유무 티어가 패널 따라 달라짐)"
    scause[d_succ & s1.isna() & s2.notna()] = "lost_successor"
    scause[d_succ & s1.notna() & s2.notna()] = "different_successor(resolver)"
    succ_causes = [[k, int(v)] for k, v in scause[d_succ].value_counts().items()]
    # --- R cause ---------------------------------------------------------------------------
    d_r = diffs["settlementReturn"]
    rcause = pd.Series("", index=common, dtype=object)
    rcause[d_r & d_end] = "endDate/정산 bar 이동"
    rcause[d_r & (rcause == "") & d_succ] = "successor 변경"
    rcause[d_r & (rcause == "") & (diffs["successorRatio"] | diffs["cashPerShare"])] = "원 조건 변경(override/파싱)"
    rcause[d_r & (rcause == "") & d_status] = "status 변경(승계사 정산 bar 가격 유무)"
    rcause[d_r & (rcause == "") & aa["settlementReturn"].notna() & bb["settlementReturn"].notna()] = "둘 다 유한·값만 다름(mark/승계사 가격일 차이)"
    rcause[d_r & (rcause == "")] = "other"
    both = d_r & aa["settlementReturn"].notna() & bb["settlementReturn"].notna()
    r_causes = [[k, int(v), fmt(float((aa["settlementReturn"] - bb["settlementReturn"])[both & (rcause == k)].abs().median()), 3)
                 if (both & (rcause == k)).any() else "-"] for k, v in rcause[d_r].value_counts().items()]
    detail = pd.DataFrame({
        "endDate_v21": aa["endDate"], "endDate_proto": bb["endDate"], "endDateSource_v21": aa["endDateSource"],
        "endDateSource_proto": bb["endDateSource"], "lastTradeDate": ltd, "endDate_cause": cause,
        "successor_v21": s1, "successor_proto": s2, "successor_cause": scause,
        "ratio_v21": aa["successorRatio"], "ratio_proto": bb["successorRatio"],
        "cash_v21": aa["cashPerShare"], "cash_proto": bb["cashPerShare"],
        "status_v21": aa["cacheUnitStatus"], "status_proto": bb["cacheUnitStatus"],
        "R_v21": aa["settlementReturn"], "R_proto": bb["settlementReturn"], "R_cause": rcause,
        "ratioCache_v21": aa["successorRatioCache"], "ratioCache_proto": bb["successorRatioCache"],
    })
    any_diff = pd.concat(diffs.values(), axis=1).any(axis=1)
    detail = detail[any_diff]
    trans = pd.crosstab(bb["cacheUnitStatus"][d_status], aa["cacheUnitStatus"][d_status]).stack()
    extra = {"end_causes": end_causes, "succ_causes": succ_causes, "r_causes": r_causes,
             "only_new": list(map(int, only_new)), "only_old": list(map(int, only_old)),
             "status_transitions": {f"{k[0]}→{k[1]}": int(v) for k, v in trans.items() if v > 0}}
    return rows, detail, extra


def gates(events: Path, close_cut: Path, close_uncut: Path, base_data: Path, lifecycle: Path, current: Path,
          proto_events: Path, lead_check: Path, out_md: Path, out_dir: Path, master_stats: dict | None,
          close_stats: dict | None, run_info: dict | None, finalize_stats: dict | None,
          split_adj_events: Path | None, split_adj_close: Path | None) -> dict:
    ev = pq.read_table(events).to_pandas()
    lc = pd.read_parquet(lifecycle)
    cur = pd.read_parquet(current)
    symbols = dict(zip(cur.tradingItemId.astype("int64"), cur.symbol))
    lcx = lc.set_index("tradingItemId")
    results = {}
    lines = [f"# T6 events 게이트 (`{events.name}`, {dt.datetime.now():%Y-%m-%d %H:%M})", "",
             f"파일 sha256 `{sha256(events)[:12]}`, 행 {len(ev):,}, 열 {len(ev.columns)}, "
             f"tradingItemId dtype `{pq.ParquetFile(events).schema_arrow.field('tradingItemId').type}`, "
             f"successorTradingItemId dtype `{pq.ParquetFile(events).schema_arrow.field('successorTradingItemId').type}`", ""]

    # --- (a) k identity -----------------------------------------------------------------------
    lines += ["## (a) 환산 항등 (raw 기준이면 k ≡ 1)", "",
              "self-test: `cashPerShareCache`를 1.001배 교란한 사본에서 위반이 검출되는지 먼저 확인.", ""]
    probe = ev.copy()
    m = probe["cashPerShareCache"].notna()
    probe.loc[m, "cashPerShareCache"] *= 1.001
    probe_rows, probe_bad = gate_k_identity(probe)
    detected = int(probe_rows[0][3])
    lines += [f"- 교란 사본: 1e-6 초과 {detected}건 검출 (교란 행 {int(m.sum())}건) → {'PASS' if detected == int(m.sum()) else 'FAIL'}", ""]
    rows, bad = gate_k_identity(ev)
    lines += [table(rows, ["항등식", "n", "최대 |편차|", "> 1e-6", "> 1e-4"]), ""]
    results["k_identity_violations_1e6"] = int(sum(r[3] for r in rows[:2]))
    results["k_probe_detected"] = detected == int(m.sum())
    if len(bad):
        bad = bad.sort_values("dev", ascending=False)
        bad_path = out_dir / "k_identity_exceptions.csv"
        bad.to_csv(bad_path, index=False)
        lines += [f"예외 {len(bad)}건 → `{bad_path.name}` (상위 15):", "",
                  table([[r.tradingItemId, symbols.get(int(r.tradingItemId), ""), fmt(r.endDate), r.which,
                          fmt(r.cacheUnitFactorTarget), fmt(r.cacheUnitFactorSuccessor), fmt(r.dev, 3), r.source]
                         for r in bad.head(15).itertuples()],
                        ["tradingItemId", "symbol", "endDate", "항등식", "k_t", "k_s", "|편차|", "source"]), ""]
    else:
        lines += ["예외 없음.", ""]
    # float32 note
    lines += ["k = as-traded(DB float64) ÷ raw close(float32 패널) 이므로 float32 표현 오차(상대 ≤ 6e-8)가 편차 상한임.", ""]

    # --- (b) lead independent check --------------------------------------------------------
    lines += ["## (b) 리드 독립검사 `check_events_contract_v3` (불변식 8종)", "",
              f"스크립트: `{lead_check}` (리드가 만든 anycache 변형: argv[2]=close 패널; 원본은 v1 캐시 경로가 하드코딩되어 raw 패널에 쓸 수 없음).", ""]
    code, text = run_lead_check(lead_check, events, close_cut)
    results["lead_check_exit_cut"] = code
    lines += [f"### 입력 패널 = lifecycle 절단 raw close (`{close_cut.name}`) → exit {code}", "", "```", text, "```", ""]
    if close_uncut is not None and close_uncut.exists():
        code2, text2 = run_lead_check(lead_check, events, close_uncut)
        results["lead_check_exit_uncut"] = code2
        lines += [f"### 반례: 절단 없는 raw close (`{close_uncut.name}`) → exit {code2}", "",
                  "꼬리(lastTradeDate 이후 승계사 가격)가 남은 패널에서는 정산 bar·mark 가드가 어긋나야 정상(파일이 절단 패널에 묶여 있음을 보임).", "",
                  "```", text2, "```", ""]

    # --- (c) compare with proto ------------------------------------------------------------
    lines += ["## (c) 기존 events_v2_altaba 대비 비교", ""]
    proto = pq.read_table(proto_events).to_pandas()
    lines += [f"proto: `{proto_events}` sha256 `{sha256(proto_events)[:12]}`, 행 {len(proto):,} (프로토 캐시(분할조정) 앵커, `--start 2009-01-01`). "
              f"v2.1: 행 {len(ev):,} (`--start 2000-01-01`), 그중 endDate ≥ 2009-01-01 {int((ev['endDate'] >= '2009-01-01').sum()):,}. "
              "비교는 공통 종목 전체(v2.1 endDate가 2009년 이전으로 당겨진 종목 포함).", ""]
    crow, detail, extra = compare_proto(ev, proto, lc)
    lines += [table(crow, ["항목", "건수", "공통 대비"]), "",
              "endDate 차이 원인(Δ = v2.1 − proto, 일):", "",
              table(extra["end_causes"], ["원인", "건수", "Δ 중앙", "Δ 최소", "Δ 최대"]), "",
              "successor 차이 원인:", "", table(extra["succ_causes"], ["원인", "건수"]), "",
              "settlementReturn 차이 원인:", "", table(extra["r_causes"], ["원인", "건수", "|ΔR| 중앙(둘 다 유한)"]), "",
              "cacheUnitStatus 전이(proto→v2.1, 상위 15):", "",
              table(sorted(extra["status_transitions"].items(), key=lambda kv: -kv[1])[:15], ["전이", "건수"]), ""]
    only_new = ev[ev["tradingItemId"].isin(extra["only_new"])]
    only_old = proto[proto["tradingItemId"].astype(str).str.strip().astype("int64").isin(extra["only_old"])]
    if len(only_new):
        on = only_new[only_new["endDate"] >= pd.Timestamp("2009-01-01")].copy()
        on["endReason"] = on["tradingItemId"].map(lcx["endReason"])
        lines += [f"v2.1에만 있는 2009+ 종목 {len(on)}건: lifecycle endReason " +
                  ", ".join(f"{k} {v}" for k, v in on["endReason"].value_counts().items()) +
                  "; endDateSource " + ", ".join(f"{k} {v}" for k, v in on["endDateSource"].value_counts().items()) +
                  " (= DB 종료일 없이 lifecycle 규칙(status 마커·10영업일 무종가)만으로 confirmed 된 종목)", ""]
        (out_dir / "only_in_v21.csv").write_text(only_new.to_csv(index=False))
    if len(only_old):
        oo = only_old.copy()
        oo["tid"] = oo["tradingItemId"].astype(str).str.strip().astype("int64")
        oo["status"] = oo["tid"].map(lcx["status"])
        lines += [f"proto에만 있는 종목 {len(only_old)}건 (lifecycle status " +
                  ", ".join(f"{k} {v}" for k, v in oo["status"].fillna("(lifecycle 없음)").value_counts().items()) + ")", ""]
        (out_dir / "only_in_proto.csv").write_text(oo.to_csv(index=False))
    detail_path = out_dir / "diff_vs_proto.csv"
    detail.to_csv(detail_path)
    lines += [f"차이 행 전체 목록 → `{detail_path.name}` ({len(detail)}건).", ""]
    results["proto_common"] = int(crow[0][1])
    results["proto_diff_endDate"] = int(sum(r[1] for r in extra["end_causes"]))
    results["proto_diff_successor"] = int(sum(r[1] for r in extra["succ_causes"]))

    # --- (d) successorCacheClose vs T2 base_data raw close --------------------------------
    lines += ["## (d) 가드 컬럼 vs T2 base_data raw close (전수)", ""]
    need_ids = set(ev.loc[ev["successorCacheClose"].notna(), "successorTradingItemId"].dropna().astype("int64")) | \
        set(ev.loc[ev["targetCacheMark"].notna(), "tradingItemId"].astype("int64"))
    base = read_base_close(base_data, need_ids)
    key = base.set_index(["tradingItemId", "date"])["close"].astype("float64")
    s = ev[ev["successorCacheClose"].notna()]
    idx = pd.MultiIndex.from_arrays([s["successorTradingItemId"].astype("int64").values, pd.to_datetime(s["successorCacheCloseDate"]).values])
    base_s = key.reindex(idx).values
    d_s = np.abs(s["successorCacheClose"].values - base_s)
    miss_s = int(np.isnan(base_s).sum())
    bad_s = int((d_s > 1e-6 * np.maximum(1.0, np.abs(base_s))).sum())
    t = ev[ev["targetCacheMark"].notna()]
    idx_t = pd.MultiIndex.from_arrays([t["tradingItemId"].astype("int64").values, pd.to_datetime(t["targetCacheMarkDate"]).values])
    base_t = key.reindex(idx_t).values
    d_t = np.abs(t["targetCacheMark"].values - base_t)
    miss_t = int(np.isnan(base_t).sum())
    bad_t = int((d_t > 1e-6 * np.maximum(1.0, np.abs(base_t))).sum())
    lines += ["self-test: successorCacheClose 첫 행을 +1% 교란 → 아래 같은 비교로 1건 검출되는지.", ""]
    if len(s):
        probe_vals = s["successorCacheClose"].values.copy(); probe_vals[0] *= 1.01
        probe_bad = int((np.abs(probe_vals - base_s) > 1e-6 * np.maximum(1.0, np.abs(base_s))).sum())
        lines += [f"- 교란 검출: {probe_bad}건 (기대 {bad_s + 1}) → {'PASS' if probe_bad == bad_s + 1 else 'FAIL'}", ""]
        results["guard_probe"] = probe_bad == bad_s + 1
    lines += [table([["successorCacheClose @ successorCacheCloseDate", len(s), miss_s, bad_s, fmt(float(np.nanmax(d_s)) if len(s) else 0.0, 3)],
                     ["targetCacheMark @ targetCacheMarkDate", len(t), miss_t, bad_t, fmt(float(np.nanmax(d_t)) if len(t) else 0.0, 3)]],
                    ["가드", "n", "base_data에 (id,date) 없음", "불일치(>1e-6 상대)", "최대 |차|"]), ""]
    results["guard_succ_mismatch"] = bad_s + miss_s
    results["guard_target_mismatch"] = bad_t + miss_t
    if bad_s + miss_s + bad_t + miss_t:
        ex = s.assign(base=base_s, diff=d_s)
        ex = ex[(np.isnan(ex["base"])) | (ex["diff"] > 1e-6)]
        lines += [table([[r.tradingItemId, r.successorTradingItemId, fmt(r.successorCacheCloseDate), fmt(r.successorCacheClose), fmt(r.base)]
                         for r in ex.head(15).itertuples()], ["tradingItemId", "successor", "date", "guard", "base_data"]), ""]

    # --- (e) BHI / CHFC / FI ------------------------------------------------------------------
    lines += ["## (e) ciq 승계사 가격 꼬리 사례: endDate = lifecycle.lastTradeDate 인지", ""]
    cut = read_wide(close_cut)
    uncut = read_wide(close_uncut) if close_uncut is not None and close_uncut.exists() else None
    rows = []
    e_ok = 0
    for tid in (2592597, 2599743, 244324100):
        r = ev[ev["tradingItemId"] == tid]
        l = lcx.loc[tid] if tid in lcx.index else None
        if not len(r) or l is None:
            rows.append([tid, symbols.get(tid, ""), "(events에 없음)" if not len(r) else "", "", "", "", "", "", "", ""])
            continue
        r = r.iloc[0]
        same = pd.Timestamp(r["endDate"]) == pd.Timestamp(l["lastTradeDate"])
        e_ok += int(same)
        bar_cut = settlement_bars(cut, [tid], [r["endDate"]])[0]
        bar_uncut = settlement_bars(uncut, [tid], [r["endDate"]])[0] if uncut is not None else pd.NaT
        rows.append([tid, symbols.get(tid, ""), fmt(l["lastTradeDate"]), l["lastTradeSource"], fmt(l["ciqLastCloseDate"]),
                     fmt(l["miLastCloseDate"]), fmt(r["endDate"]), fmt(r["endDateOriginal"]), r["endDateSource"],
                     "O" if same else "X", fmt(bar_cut), fmt(bar_uncut), r["successorTradingItemId"], r["cacheUnitStatus"]])
    lines += [table(rows, ["tradingItemId", "symbol", "lifecycle lastTradeDate", "src", "ciqLastClose", "miLastClose",
                           "endDate", "endDateOriginal", "endDateSource", "일치", "정산 bar(절단)", "정산 bar(미절단)",
                           "successor", "status"]), ""]
    results["tail_cases_ok"] = e_ok
    # whole-file: how many rows have endDate == lastTradeDate; and rows where endDate > lastTradeDate (should be 0 with cut panel)
    ltd = ev["tradingItemId"].map(lcx["lastTradeDate"])
    eq = (ev["endDate"] == ltd)
    gt = (ev["endDate"] > ltd)
    lt = (ev["endDate"] < ltd)
    lines += [table([["endDate == lastTradeDate", int(eq.sum())], ["endDate > lastTradeDate", int(gt.sum())],
                     ["endDate < lastTradeDate", int(lt.sum())],
                     ["  그중 override", int((lt & ev["endDateSource"].str.startswith("override")).sum())],
                     ["  그중 reviewFlag enddate_gap 아님·override 아님", int((lt & ~ev["endDateSource"].str.startswith("override") & ~ev["reviewFlag"].str.contains("enddate_gap")).sum())]],
                    ["전체 파일", "행수"]), ""]
    if gt.any():
        g = ev[gt].assign(lastTradeDate=ltd[gt])
        gpath = out_dir / "enddate_after_lastTradeDate.csv"
        g.to_csv(gpath, index=False)
        lines += [f"endDate > lastTradeDate 행 → `{gpath.name}` (endDateSource: " +
                  ", ".join(f"{k} {v}" for k, v in g["endDateSource"].value_counts().items()) + ")", "",
                  table([[r.tradingItemId, symbols.get(int(r.tradingItemId), ""), fmt(r.endDate), fmt(r.lastTradeDate),
                          r.endDateSource, r.reviewFlag, r.cacheUnitStatus] for r in g.head(15).itertuples()],
                        ["tradingItemId", "symbol", "endDate", "lastTradeDate", "endDateSource", "reviewFlag", "status"]), ""]
    if lt.any():
        g = ev[lt].assign(lastTradeDate=ltd[lt], gap=(ltd[lt] - ev.loc[lt, "endDate"]).dt.days)
        lines += [f"endDate < lastTradeDate {int(lt.sum())}건: 갭 중앙 {int(g['gap'].median())}일·p95 {int(g['gap'].quantile(.95))}일·최대 {int(g['gap'].max())}일; "
                  f"endDateSource: " + ", ".join(f"{k} {v}" for k, v in g["endDateSource"].value_counts().head(6).items()), ""]
        (out_dir / "enddate_before_lastTradeDate.csv").write_text(g.to_csv(index=False))
    results["endDate_gt_lastTradeDate"] = int(gt.sum())

    # --- (f) split_adj counter-example -------------------------------------------------------
    lines += ["## (f) 반례 self-test: split_adj 패널로 생성하면 k≠1 이 다수 검출되는가", ""]
    if split_adj_events is not None and split_adj_events.exists():
        sa = pq.read_table(split_adj_events).to_pandas()
        srows, sbad = gate_k_identity(sa)
        lines += [f"split_adj 패널 (`{split_adj_close}`) + 같은 마스터·override 로 build_events 실행 → `{split_adj_events.name}` {len(sa):,}행.", "",
                  table(srows, ["항등식", "n", "최대 |편차|", "> 1e-6", "> 1e-4"]), ""]
        kt = sa["cacheUnitFactorTarget"].dropna()
        ks = sa["cacheUnitFactorSuccessor"].dropna()
        lines += [f"k_t ≠ 1(1e-6): {int(((kt - 1).abs() > 1e-6).sum())}/{len(kt)}, k_s ≠ 1: {int(((ks - 1).abs() > 1e-6).sum())}/{len(ks)}; "
                  f"k_t 값 분포(상위): " + ", ".join(f"{v:g}×{c}" for v, c in kt.round(4).value_counts().head(8).items()), ""]
        results["split_adj_k_ne_1"] = int(((kt - 1).abs() > 1e-6).sum())
    else:
        lines += ["(split_adj 실행 없음)", ""]

    # --- lineage summary --------------------------------------------------------------------
    lines += ["## lineage 승계 행", ""]
    lin_rows = ev[ev["matchMethod"] == "lineage"]
    lines += [f"matchMethod=lineage 행 {len(lin_rows)}건: source " +
              ", ".join(f"{k} {v}" for k, v in lin_rows["source"].value_counts().items()) + "; cacheUnitStatus " +
              ", ".join(f"{k} {v}" for k, v in lin_rows["cacheUnitStatus"].value_counts().items()), ""]
    if finalize_stats:
        lines += ["finalize 통계:", "", "```", json.dumps(finalize_stats, indent=1, default=str), "```", ""]

    # --- distributions -----------------------------------------------------------------------
    lines += ["## 분포", ""]
    lines += ["actionCode:", "", table([[k, v] for k, v in ev["actionCode"].fillna("(none)").value_counts().items()], ["actionCode", "행수"]), "",
              "cacheUnitStatus:", "", table([[k, v] for k, v in ev["cacheUnitStatus"].value_counts().items()], ["cacheUnitStatus", "행수"]), "",
              "source:", "", table([[k, v] for k, v in ev["source"].value_counts().items()], ["source", "행수"]), "",
              "endDateSource:", "", table([[k, v] for k, v in ev["endDateSource"].value_counts().items()], ["endDateSource", "행수"]), "",
              "endDateBasis:", "", table([[k, v] for k, v in ev["endDateBasis"].value_counts().items()], ["endDateBasis", "행수"]), "",
              f"hasStockLeg {int(ev['hasStockLeg'].sum())}, hasCashLeg {int(ev['hasCashLeg'].sum())}, 둘 다 {int((ev['hasStockLeg'] & ev['hasCashLeg']).sum())}, "
              f"settlementReturn 유한 {int(np.isfinite(ev['settlementReturn']).sum())}, successor 있음 {int(ev['successorTradingItemId'].notna().sum())}", ""]
    yr = ev["endDate"].dt.year.value_counts().sort_index()
    lines += ["endDate 연도별:", "", table([[y, n] for y, n in yr.items()], ["연도", "행수"]), ""]

    # inputs
    lines += ["## 입력·실행 기록", ""]
    for name, obj in (("master", master_stats), ("close", close_stats), ("run", run_info)):
        if obj:
            lines += [f"{name}:", "", "```", json.dumps(obj, indent=1, default=str), "```", ""]
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out_dir / "gates.json").write_text(json.dumps(results, indent=1, default=str))
    log(f"gates: {json.dumps(results, default=str)}")
    return results


# ------------------------------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare-master"); p.add_argument("--t1", default=str(STAGE / "T1")); p.add_argument("--out", required=True)
    p = sub.add_parser("prepare-close"); p.add_argument("--raw-close", required=True); p.add_argument("--lifecycle", required=True)
    p.add_argument("--out", required=True); p.add_argument("--no-cut", action="store_true")
    p = sub.add_parser("run"); p.add_argument("--master", required=True); p.add_argument("--close", required=True)
    p.add_argument("--override", required=True); p.add_argument("--out", required=True)
    p.add_argument("--universe-dir", default=str(PROTO_UNIVERSE_DIR)); p.add_argument("--skip-universe", action="store_true")
    p.add_argument("--start", default="2000-01-01"); p.add_argument("--log", required=True)
    p.add_argument("extra", nargs="*")
    p = sub.add_parser("finalize"); p.add_argument("--built", required=True); p.add_argument("--lineage", required=True)
    p.add_argument("--lifecycle", required=True); p.add_argument("--current", required=True); p.add_argument("--close", required=True)
    p.add_argument("--out", required=True); p.add_argument("--lineage-csv", required=True); p.add_argument("--start", default="2000-01-01")
    p.add_argument("--mode", choices=["add-and-fill", "add-only"], default="add-and-fill")
    p = sub.add_parser("reclass"); p.add_argument("--events", required=True); p.add_argument("--reclass", required=True)
    p.add_argument("--lifecycle", required=True); p.add_argument("--current", required=True); p.add_argument("--close", required=True)
    p.add_argument("--out", required=True); p.add_argument("--start", default="2000-01-01"); p.add_argument("--no-estimates", action="store_true")
    p = sub.add_parser("gates"); p.add_argument("--events", required=True); p.add_argument("--close-cut", required=True)
    p.add_argument("--close-uncut", default=None); p.add_argument("--base-data", default=str(STAGE / "T2" / "base_data.parquet"))
    p.add_argument("--lifecycle", required=True); p.add_argument("--current", required=True)
    p.add_argument("--proto-events", default=str(PROTO_EVENTS_DIR / "events.parquet"))
    p.add_argument("--lead-check", default=str(PROTO_EVENTS_DIR / "check_events_contract_v3_anycache.py"))
    p.add_argument("--out-md", required=True); p.add_argument("--out-dir", required=True)
    p.add_argument("--stats-json", default=None, help="json with master/close/run/finalize stats from earlier steps")
    p.add_argument("--split-adj-events", default=None); p.add_argument("--split-adj-close", default=None)
    a = ap.parse_args(argv)
    if a.cmd == "prepare-master":
        s = prepare_master(Path(a.t1), Path(a.out)); print(json.dumps(s, indent=1, default=str)); return 0
    if a.cmd == "prepare-close":
        s = prepare_close(Path(a.raw_close), Path(a.lifecycle), Path(a.out), cut=not a.no_cut)
        print(json.dumps(s, indent=1, default=str)); return 0
    if a.cmd == "run":
        s = run_build_events(Path(a.master), Path(a.close), Path(a.override), Path(a.out),
                             None if a.skip_universe else Path(a.universe_dir), a.start, a.extra, Path(a.log))
        print(json.dumps(s, indent=1, default=str)); return 0
    if a.cmd == "finalize":
        s = finalize(Path(a.built), Path(a.lineage), Path(a.lifecycle), Path(a.current), Path(a.close), Path(a.out),
                     Path(a.lineage_csv), a.start, a.mode)
        print(json.dumps(s, indent=1, default=str)); return 0
    if a.cmd == "reclass":
        s = apply_reclassification(Path(a.events), Path(a.reclass), Path(a.lifecycle), Path(a.current), Path(a.close), Path(a.out),
                                   a.start, include_estimates=not a.no_estimates)
        print(json.dumps(s, indent=1, default=str)); return 0
    if a.cmd == "gates":
        stats = json.loads(Path(a.stats_json).read_text()) if a.stats_json else {}
        r = gates(Path(a.events), Path(a.close_cut), Path(a.close_uncut) if a.close_uncut else None, Path(a.base_data),
                  Path(a.lifecycle), Path(a.current), Path(a.proto_events), Path(a.lead_check), Path(a.out_md), Path(a.out_dir),
                  stats.get("master"), stats.get("close"), stats.get("run"), stats.get("finalize"),
                  Path(a.split_adj_events) if a.split_adj_events else None,
                  Path(a.split_adj_close) if a.split_adj_close else None)
        print(json.dumps(r, indent=1, default=str)); return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
