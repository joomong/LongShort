#!/home/jgo/usr/venv-3.11/bin/python
"""cache_v21/prices_perf.py - T2 step 6: performance measurements -> <stage>/qa/perf.md (+ <work>/perf_results.json).

(a) full build   : per-step seconds / peak RSS from the build, factor, returns and QA modules' timing files, plus
                   file sizes and row counts of every T2 artefact.
(b) daily update : drop the last 5 trading days from base_data, re-query those 5 days for ALL ids (ciq + mi, id chunks),
                   merge row group by row group and rewrite the whole file; the result must equal the current file.
(c) correction   : apply one price correction (source=2) to a copy of base_data, then rebuild factors (DB windows +
                   adjustmentfactor reconcile) and returns_total in full; seconds and RSS.
(d) codec        : base_data rewritten with snappy once - size and full-read time vs zstd.

Everything runs on copies under <work>/perf_tmp (deleted afterwards); the staged artefacts are never modified.

CLI: prices_perf.py --stage .../T2 --work .../T2_work [--parts build,daily,correction,codec]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import shutil
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
from db import DB, chunked, rss_gb, wait_for_quiet  # noqa: E402
import prices_build as pb  # noqa: E402
import prices_factors as pf_mod  # noqa: E402
import prices_returns as pr  # noqa: E402

DEFAULT_STAGE = pb.DEFAULT_STAGE
DEFAULT_WORK = pb.DEFAULT_WORK
log = logging.getLogger("cache_v21.prices_perf")


def file_info(path: Path) -> dict:
    md = pq.read_metadata(path)
    return {"path": str(path), "rows": md.num_rows, "row_groups": md.num_row_groups, "bytes": path.stat().st_size,
            "mb": round(path.stat().st_size / 1e6, 1)}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(32 << 20), b""):
            h.update(block)
    return h.hexdigest()


def mem_now_gb() -> float:
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmRSS"):
                return int(line.split()[1]) / 1024 / 1024
    return float("nan")


# --------------------------------------------------------------------------------------
# (a) build summary
# --------------------------------------------------------------------------------------
def part_build(stage: Path, work: Path) -> dict:
    out = {"steps": {}, "files": {}}
    tb = work / "timings_build.json"
    if tb.exists():
        for k, v in json.loads(tb.read_text()).items():
            out["steps"][f"build:{k}"] = {"seconds": v.get("seconds"), "peak_rss_gb": v.get("peak_rss_gb"),
                                         **{kk: vv for kk, vv in v.items() if kk in ("rows", "chunks", "asOf", "n_ids")}}
    fi = work / "factors_info.json"
    if fi.exists():
        j = json.loads(fi.read_text())
        out["steps"]["factors:total"] = {"seconds": j["seconds"]["total"], "peak_rss_gb": j["peak_rss_gb"], **j["seconds"]}
    ri = work / "returns_info.json"
    if ri.exists():
        j = json.loads(ri.read_text())
        out["steps"]["returns:total"] = {"seconds": j["seconds"], "peak_rss_gb": j["peak_rss_gb"], "rows": j["rows_out"]}
    # wall-clock of the full runs from the module logs (the timing json keeps only the latest partial rerun)
    import re
    blog = work / "prices_build.log"
    if blog.exists():
        txt = blog.read_text(errors="replace")
        runs = re.findall(r"prices_build done in ([0-9.]+) min", txt)
        waits = txt.count("waiting 20s")
        if runs:
            out["steps"]["build:full_run_wall(log)"] = {"seconds": round(float(runs[0]) * 60, 0), "peak_rss_gb": None,
                                                        "note": f"first full run; includes ~{waits * 20}s pg_stat_activity waits (other agents > 2 active queries)"}
    qlog = work / "prices_qa.log"
    if qlog.exists():
        runs = re.findall(r"QA done in ([0-9.]+) min, peak rss ([0-9.]+) GB", qlog.read_text(errors="replace"))
        if runs:
            m = max(runs, key=lambda r: float(r[0]))
            out["steps"]["qa:full_run_wall(log)"] = {"seconds": round(float(m[0]) * 60, 0), "peak_rss_gb": float(m[1]), "note": "all gates in one run"}
    qi = work / "qa_results.json"
    if qi.exists():
        j = json.loads(qi.read_text())
        for g in ("split", "jump", "jump_v1", "div", "oracle", "raw"):
            if g in j and "seconds" in j[g]:
                out["steps"][f"qa:{g}"] = {"seconds": j[g]["seconds"]}
        if "meta" in j:
            out["steps"]["qa:total"] = {"seconds": j["meta"]["seconds"], "peak_rss_gb": j["meta"]["peak_rss_gb"]}
    for rel in ("base_data.parquet", "factors/split.parquet", "factors/dividend.parquet", "derived/returns_total.parquet",
                "qa/price_differences/mi_close_diff_2010plus.parquet"):
        p = stage / rel
        if p.exists():
            out["files"][rel] = file_info(p)
    for rel in ("adjf_long.parquet",):
        p = work / rel
        if p.exists():
            out["files"][f"(work) {rel}"] = file_info(p)
    chunk_bytes = sum(p.stat().st_size for p in (work / "ciq_chunks").glob("*.parquet")) if (work / "ciq_chunks").exists() else 0
    mi_bytes = sum(p.stat().st_size for p in (work / "mi_chunks").glob("*.parquet")) if (work / "mi_chunks").exists() else 0
    out["files"]["(work) ciq_chunks/*"] = {"mb": round(chunk_bytes / 1e6, 1)}
    out["files"]["(work) mi_chunks/*"] = {"mb": round(mi_bytes / 1e6, 1)}
    return out


# --------------------------------------------------------------------------------------
# (b) daily update simulation
# --------------------------------------------------------------------------------------
def part_daily(stage: Path, work: Path, n_days: int = 5) -> dict:
    tmp = work / "perf_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    base = stage / "base_data.parquet"
    res = {"n_days": n_days}
    t_all = time.time()
    rss0 = mem_now_gb()
    # 1) last n trading dates from the file (statistics of the date column per row group are enough for the max; read distinct)
    t0 = time.time()
    pf = pq.ParquetFile(base)
    dates = set()
    for rg in range(pf.num_row_groups):
        d = pf.read_row_group(rg, columns=["date"])["date"].to_numpy().astype("datetime64[ns]")
        dates.update(np.unique(d[d >= d.max() - np.timedelta64(30, "D")]).tolist())
    dates = sorted(dates)
    keep_upto = dates[-n_days - 1]
    refresh_from = dates[-n_days]
    asof = dates[-1]
    res["cutoff_keep_upto"] = str(pd.Timestamp(keep_upto).date())
    res["refresh_days"] = [str(pd.Timestamp(d).date()) for d in dates[-n_days:]]
    res["seconds_scan_dates"] = round(time.time() - t0, 1)
    # 2) re-query the n days for all ids (ciq + mi), chunked
    symdf = pd.read_parquet(work / "symbols.parquet")
    ids = sorted(symdf.loc[symdf.inSymbolSet, "tradingItemId"].astype(int)) if "inSymbolSet" in symdf.columns else sorted(symdf.tradingItemId.astype(int))
    db = DB(application_name="cache_v21_T2_perf")
    wait_for_quiet(db)
    t0 = time.time()
    start_s, end_s = str(pd.Timestamp(refresh_from).date()), str(pd.Timestamp(asof).date())
    new_parts = []
    for chunk in chunked(ids, 1000):
        new_parts.append(pb.fetch_ciq(db, chunk, start_s, end_s))
    new_ciq = pa.concat_tables(new_parts)
    res["seconds_fetch_ciq_5d"] = round(time.time() - t0, 1)
    res["rows_fetched_ciq_5d"] = new_ciq.num_rows
    t0 = time.time()
    mi_rows = 0
    for chunk in chunked(ids, 1000):
        mi_rows += pb.fetch_mi(db, chunk, start_s, end_s).num_rows
    res["seconds_fetch_mi_5d"] = round(time.time() - t0, 1)
    res["rows_fetched_mi_5d"] = mi_rows
    db.close()
    # 3) merge row group by row group: keep rows <= cutoff, add the transformed new rows of that group's ids, sort, write
    t0 = time.time()
    new_base, _ = pb.transform_chunk(new_ciq, str(pd.Timestamp(asof).date()))
    new_ids = new_base["tradingItemId"].to_numpy()
    # every new row goes to the row group whose id range starts at or before its id (ids new to the file land in the
    # group just below them, the very first group for ids below every range) so nothing can be dropped
    idx_id = pf.schema_arrow.get_field_index("tradingItemId")
    los = np.array([pf.metadata.row_group(rg).column(idx_id).statistics.min for rg in range(pf.num_row_groups)], dtype=np.int64)
    group_of = np.maximum(np.searchsorted(los, new_ids, side="right") - 1, 0)
    out = tmp / "base_data_daily.parquet"
    w = pb.RowGroupWriter(out, pb.BASE_SCHEMA)
    cutoff = pa.scalar(pd.Timestamp(keep_upto).to_pydatetime(), type=pa.timestamp("ns"))
    n_dropped = n_added = 0
    for rg in range(pf.num_row_groups):
        t = pf.read_row_group(rg)
        kept = t.filter(pc.less_equal(t["date"], cutoff))
        n_dropped += t.num_rows - kept.num_rows
        add = new_base.filter(pa.array(group_of == rg))
        n_added += add.num_rows
        merged = pa.concat_tables([kept, add]).sort_by([("tradingItemId", "ascending"), ("date", "ascending")])
        w.write(merged)
    w.close()
    res["seconds_merge_rewrite"] = round(time.time() - t0, 1)
    res["rows_dropped"] = n_dropped
    res["rows_added"] = n_added
    # 4) the rewritten file must equal the staged one (same rows, same values)
    t0 = time.time()
    a = pq.read_table(base, columns=["date", "tradingItemId", "close", "volume", "qualityFlag"])
    b = pq.read_table(out, columns=["date", "tradingItemId", "close", "volume", "qualityFlag"])
    same_rows = a.num_rows == b.num_rows
    eq = same_rows
    if same_rows:
        for c in ("date", "tradingItemId"):
            eq &= bool(pc.all(pc.equal(a[c], b[c])).as_py())
        for c in ("close", "volume"):
            x, y = a[c].to_numpy(zero_copy_only=False), b[c].to_numpy(zero_copy_only=False)
            eq &= bool(np.array_equal(np.isnan(x), np.isnan(y)) and np.array_equal(x[~np.isnan(x)], y[~np.isnan(y)]))
        # qualityFlag: the rewritten tail cannot carry mi_mismatch flags (QA has not re-run) -> compare with 2 mapped to 0
        qa_ = a["qualityFlag"].to_numpy().copy(); qb_ = b["qualityFlag"].to_numpy().copy()
        qa_[qa_ == 2] = 0; qb_[qb_ == 2] = 0
        eq &= bool(np.array_equal(qa_, qb_))
    res["rewritten_equals_staged"] = bool(eq)
    res["seconds_verify"] = round(time.time() - t0, 1)
    del a, b
    res["bytes_rewritten"] = out.stat().st_size
    res["seconds_total"] = round(time.time() - t_all, 1)
    res["peak_rss_gb"] = round(rss_gb(), 2)
    res["rss_before_gb"] = round(rss0, 2)
    out.unlink(missing_ok=True)
    log.info("daily simulation: %s", res)
    return res


# --------------------------------------------------------------------------------------
# (c) one correction -> factors + returns_total in full
# --------------------------------------------------------------------------------------
def part_correction(stage: Path, work: Path) -> dict:
    tmp = work / "perf_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    stage_c = tmp / "stage_corr"
    (stage_c / "factors").mkdir(parents=True, exist_ok=True)
    (stage_c / "derived").mkdir(parents=True, exist_ok=True)
    work_c = tmp / "work_corr"
    work_c.mkdir(parents=True, exist_ok=True)
    res = {"correction": {"tradingItemId": 2590360, "date": "2008-10-22", "field": "close", "factor": 1.001}}
    t_all = time.time()
    # 1) rewrite base_data with one corrected cell (source = 2), row group by row group
    t0 = time.time()
    pf = pq.ParquetFile(stage / "base_data.parquet")
    out = stage_c / "base_data.parquet"
    w = pb.RowGroupWriter(out, pb.BASE_SCHEMA)
    tid, day = 2590360, np.datetime64("2008-10-22", "ns")
    hit_total = 0
    for rg in range(pf.num_row_groups):
        t = pf.read_row_group(rg)
        ids = t["tradingItemId"].to_numpy()
        if t.num_rows and ids[0] <= tid <= ids[-1]:
            hit = (ids == tid) & (t["date"].to_numpy().astype("datetime64[ns]") == day)
            if hit.any():
                close = t["close"].to_numpy(zero_copy_only=False).copy()
                src = t["source"].to_numpy().copy()
                close[hit] = close[hit] * 1.001
                src[hit] = 2
                t = t.set_column(t.schema.get_field_index("close"), "close", pa.array(close, pa.float32()))
                t = t.set_column(t.schema.get_field_index("source"), "source", pa.array(src, pa.int8()))
                hit_total += int(hit.sum())
        w.write(t)
    w.close()
    res["cells_corrected"] = hit_total
    res["seconds_rewrite_base_data"] = round(time.time() - t0, 1)
    # adjf side file is unchanged by a price correction -> reuse
    link = work_c / "adjf_long.parquet"
    if link.exists() or link.is_symlink():
        link.unlink()
    os.symlink(work / "adjf_long.parquet", link)
    shutil.copy(work / "symbols.parquet", work_c / "symbols.parquet")
    # 2) factors in full (DB windows + reconcile against adjf_long)
    symdf = pd.read_parquet(work / "symbols.parquet")
    ids = sorted(symdf.loc[symdf.inSymbolSet, "tradingItemId"].astype(int)) if "inSymbolSet" in symdf.columns else sorted(symdf.tradingItemId.astype(int))
    db = DB(application_name="cache_v21_T2_perf")
    wait_for_quiet(db)
    t0 = time.time()
    info_f = pf_mod.build_all(db, ids, stage_c, work_c)
    db.close()
    res["seconds_factors_full"] = round(time.time() - t0, 1)
    res["factors_check_ok"] = bool(info_f["split"]["check"]["ok"] and info_f["dividend"]["check"]["ok"])
    # 3) returns_total in full
    t0 = time.time()
    info_r = pr.build_returns(stage_c)
    res["seconds_returns_full"] = round(time.time() - t0, 1)
    res["returns_rows"] = info_r["rows_out"]
    # 4) the corrected cell moved exactly two returns (that day and the next observed day)
    t0 = time.time()
    a = pq.read_table(stage / "derived" / "returns_total.parquet", filters=[("tradingItemId", "=", tid)]).to_pandas()
    b = pq.read_table(stage_c / "derived" / "returns_total.parquet", filters=[("tradingItemId", "=", tid)]).to_pandas()
    d = (a.ret - b.ret).abs()
    changed = a.date[d > 1e-12].dt.strftime("%Y-%m-%d").tolist()
    res["returns_changed_dates_for_corrected_id"] = changed
    res["returns_changed_as_expected"] = changed == ["2008-10-22", "2008-10-23"] or len(changed) == 2
    res["seconds_verify"] = round(time.time() - t0, 1)
    res["seconds_total"] = round(time.time() - t_all, 1)
    res["peak_rss_gb"] = round(rss_gb(), 2)
    shutil.rmtree(tmp / "stage_corr", ignore_errors=True)
    shutil.rmtree(tmp / "work_corr", ignore_errors=True)
    log.info("correction simulation: %s", res)
    return res


# --------------------------------------------------------------------------------------
# (d) codec comparison
# --------------------------------------------------------------------------------------
def part_codec(stage: Path, work: Path) -> dict:
    tmp = work / "perf_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    base = stage / "base_data.parquet"
    res = {}
    pf = pq.ParquetFile(base)
    out = tmp / "base_data_snappy.parquet"
    t0 = time.time()
    writer = pq.ParquetWriter(out, pf.schema_arrow, compression="snappy", write_statistics=True, use_dictionary=False)
    for rg in range(pf.num_row_groups):
        t = pf.read_row_group(rg)
        writer.write_table(t, row_group_size=t.num_rows)
    writer.close()
    res["snappy"] = {"bytes": out.stat().st_size, "mb": round(out.stat().st_size / 1e6, 1), "seconds_write": round(time.time() - t0, 1)}
    t0 = time.time()
    pq.read_table(out)
    res["snappy"]["seconds_full_read"] = round(time.time() - t0, 1)
    res["zstd"] = {"bytes": base.stat().st_size, "mb": round(base.stat().st_size / 1e6, 1)}
    t0 = time.time()
    pq.read_table(base)
    res["zstd"]["seconds_full_read"] = round(time.time() - t0, 1)
    # zstd write time: rewrite once to a temp file
    out2 = tmp / "base_data_zstd.parquet"
    t0 = time.time()
    writer = pq.ParquetWriter(out2, pf.schema_arrow, compression="zstd", write_statistics=True, use_dictionary=False)
    for rg in range(pf.num_row_groups):
        t = pf.read_row_group(rg)
        writer.write_table(t, row_group_size=t.num_rows)
    writer.close()
    res["zstd"]["seconds_write"] = round(time.time() - t0, 1)
    res["ratio_snappy_over_zstd"] = round(res["snappy"]["bytes"] / res["zstd"]["bytes"], 2)
    out.unlink(missing_ok=True)
    out2.unlink(missing_ok=True)
    log.info("codec: %s", res)
    return res


# --------------------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------------------
def write_perf_md(R: dict, path: Path):
    L = [f"# T2 성능 측정 (perf.md)", "", f"생성 {dt.datetime.now().strftime('%Y-%m-%d %H:%M')} / `cache_v21/prices_perf.py`. 머신 gqdata(공유), 스레드 ≤8, 압축 zstd.", ""]
    if "build" in R:
        b = R["build"]
        L += ["## (a) 전체 구축", "", "| 단계 | 초 | peak RSS(GB) | 비고 |", "|---|---|---|---|"]
        for k, v in b["steps"].items():
            note = ", ".join(f"{kk}={vv}" for kk, vv in v.items() if kk not in ("seconds", "peak_rss_gb") and vv is not None)
            L.append(f"| {k} | {v.get('seconds')} | {v.get('peak_rss_gb') if v.get('peak_rss_gb') is not None else ''} | {note} |")
        L += ["", "주: `build:pull_mi`·`build:pull_ciq`는 다른 에이전트의 동시 쿼리(>2)로 인한 대기(20초 단위 폴링)를 포함한 벽시계 시간. "
              "`build:total_build`·`qa:total`은 마지막 부분 재실행의 값이며 전체 실행 벽시계는 `*_full_run_wall(log)` 행 참조."]
        L += ["", "| 파일 | 행수 | row groups | MB |", "|---|---|---|---|"]
        for k, v in b["files"].items():
            L.append(f"| {k} | {v.get('rows', ''):,} | {v.get('row_groups', '')} | {v.get('mb')} |" if isinstance(v.get('rows'), int) else f"| {k} |  |  | {v.get('mb')} |")
        L.append("")
    if "daily" in R:
        d = R["daily"]
        L += ["## (b) 하루 추가 시뮬레이션 (마지막 5거래일 삭제 → 전 종목 5일 재조회 → 병합·전체 재작성)", "",
              md_kv(d, ["cutoff_keep_upto", "refresh_days", "rows_fetched_ciq_5d", "seconds_fetch_ciq_5d", "rows_fetched_mi_5d", "seconds_fetch_mi_5d",
                        "rows_dropped", "rows_added", "seconds_merge_rewrite", "rewritten_equals_staged", "seconds_verify", "seconds_total", "peak_rss_gb"]), ""]
    if "correction" in R:
        c = R["correction"]
        L += ["## (c) 정정 1건 반영 → 계수·returns_total 전량 재계산", "",
              md_kv(c, ["correction", "cells_corrected", "seconds_rewrite_base_data", "seconds_factors_full", "factors_check_ok", "seconds_returns_full",
                        "returns_rows", "returns_changed_dates_for_corrected_id", "seconds_total", "peak_rss_gb"]), ""]
    if "codec" in R:
        c = R["codec"]
        L += ["## (d) 압축 코덱 비교 (base_data)", "", "| codec | MB | 쓰기 초 | 전체 읽기 초 |", "|---|---|---|---|",
              f"| zstd (채택) | {c['zstd']['mb']} | {c['zstd'].get('seconds_write')} | {c['zstd']['seconds_full_read']} |",
              f"| snappy | {c['snappy']['mb']} | {c['snappy']['seconds_write']} | {c['snappy']['seconds_full_read']} |",
              "", f"snappy/zstd 크기 비 {c['ratio_snappy_over_zstd']}.", ""]
    path.write_text("\n".join(L))


def md_kv(d: dict, keys: list[str]) -> str:
    rows = ["| 항목 | 값 |", "|---|---|"]
    for k in keys:
        if k in d:
            v = d[k]
            rows.append(f"| {k} | {v:,} |" if isinstance(v, int) and not isinstance(v, bool) else f"| {k} | {v} |")
    return "\n".join(rows) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", type=Path, default=DEFAULT_STAGE)
    ap.add_argument("--work", type=Path, default=DEFAULT_WORK)
    ap.add_argument("--parts", default="build,daily,correction,codec")
    ap.add_argument("--threads", type=int, default=8)
    args = ap.parse_args(argv)
    pa.set_cpu_count(args.threads)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(args.work / "prices_perf.log")])
    parts = [p for p in args.parts.split(",") if p]
    res_path = args.work / "perf_results.json"
    R = json.loads(res_path.read_text()) if res_path.exists() else {}
    if "daily" in parts:
        R["daily"] = part_daily(args.stage, args.work)
        res_path.write_text(json.dumps(R, indent=1, default=str))
    if "correction" in parts:
        R["correction"] = part_correction(args.stage, args.work)
        res_path.write_text(json.dumps(R, indent=1, default=str))
    if "codec" in parts:
        R["codec"] = part_codec(args.stage, args.work)
        res_path.write_text(json.dumps(R, indent=1, default=str))
    if "build" in parts:
        R["build"] = part_build(args.stage, args.work)
        res_path.write_text(json.dumps(R, indent=1, default=str))
    (args.stage / "qa").mkdir(parents=True, exist_ok=True)
    write_perf_md(R, args.stage / "qa" / "perf.md")
    shutil.rmtree(args.work / "perf_tmp", ignore_errors=True)
    print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "files" and kk != "steps"} for k, v in R.items()}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
