#!/home/jgo/usr/venv-3.11/bin/python
"""D1 게이트: 증분 체인 vs 전체 재생성 비트 동일(orch_daily/BRIEF_D.md D1).

  체인  : baseline(rc2f L2 R1, asOf 09-15) --incremental --source-cut 09-10 → s0910 → 09-11 → 09-14 → 09-15 (전날 스테이지가 --prev)
  전체  : 날짜마다 prepare-full(baseline + 같은 추출 + cut, 한 번에) → ledger_build.py --no-db --closes-max-date D (같은 인자)
  '그 날까지의 원천' = 오늘 추출한 xfeed 원천을 사건일(exDate/exdate/splitdate/dividenddate/pricedate) ≤ D 로 자른 것 + 캘린더 거래일 ≤ D +
  raw 종가 ≤ D. factors/events/lineage/lifecycle 은 두 경로 모두 L2 스냅샷(다른 U-단계 산출물; 두 경로에 같은 파일).
  비교  : 원장 행을 (tradingItemId, exDate, eventType, legType, childTradingItemId[, seq]) 로 정렬해 컬럼별 NaN-aware 비트 동일 + 파일 sha256.
          eventId 포함(둘 다 같은 규칙으로 부여). 다르면 행 목록·귀속을 <out>/diff_<date>.csv 에.

사용: gate_ledger_incremental.py --out /home/jgo/longshort/daily_D1 [--dates 2026-09-11,2026-09-14,2026-09-15] [--start 2026-09-10]
      [--skip-chain] [--skip-full] [--full-mode refresh|scratch|both] [--jobs 3]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import ledger_build as lb  # noqa: E402
import ledger_incremental as li  # noqa: E402

PY = "/home/jgo/usr/venv-3.11/bin/python"
D = HERE.parent
L2 = Path("/home/jgo/longshort/rc2f_L2/build")
SP = lb.SCRATCH


def run(cmd: list, log: Path) -> tuple[int, float]:
    t0 = time.time()
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "w") as fh:
        rc = subprocess.run([str(c) for c in cmd], stdout=fh, stderr=subprocess.STDOUT, cwd=str(D)).returncode
    return rc, time.time() - t0


def chain_step(prev: Path, asof: str, out: Path, extract: Path, bootstrap: bool, log: Path, calendar: Path) -> tuple[int, float]:
    # the calendar is a new-build input (U2): in the gate every step takes the baseline's full calendar and cuts it at asof (the previous stage's is cut)
    cmd = [PY, D / "ledger_build.py", "--incremental", "--prev", prev, "--asof", asof, "--out", out, "--source-extract", extract, "--source-cut", asof, "--calendar", calendar]
    if bootstrap:
        cmd += ["--allow-asof-before-prev", "--no-qa"]
    return run(cmd, log)


def full_build(baseline: Path, extract: Path, asof: str, out: Path, full_mode: str, log: Path) -> tuple[int, float]:
    rc, t1 = run([PY, D / "ledger_incremental.py", "prepare-full", "--baseline", baseline, "--extract", extract, "--cut", asof, "--out", out, "--full-mode", full_mode], log.with_name(log.name + ".prepare"))
    if rc:
        return rc, t1
    side = json.loads((baseline / "events" / "ca_ledger_build_info.json").read_text())["inputs"]
    ovr = sorted((out / "overrides").glob("*.csv"))
    cmd = [PY, D / "ledger_build.py", "--root", out / "root", "--stage", out, "--base-data", side["base_data"], "--no-db", "--lineage", side["lineage"], "--lifecycle", side["lifecycle"],
           "--aux-base-data", side["aux_base_data"], "--r3-events", side["r3_events"], "--closes-max-date", asof,
           "--overrides-ref-ledger", SP / "build_rc2_final" / "events" / "ca_ledger.parquet"]
    if ovr:
        cmd += ["--overrides"] + ovr
    for name, flag in (("split_boundary_moves.csv", "--split-moves"), ("price_override_estimates.csv", "--estimated-prices")):
        p = baseline / name
        if p.exists():
            cmd += [flag, p]
    rc, t2 = run(cmd, log)
    return rc, t1 + t2


def compare(a_path: Path, b_path: Path, out_csv: Path) -> dict:
    A, B = pd.read_parquet(a_path), pd.read_parquet(b_path)
    A["_k"], B["_k"] = li._ledger_keys(A).to_numpy(), li._ledger_keys(B).to_numpy()
    A, B = A.sort_values("_k", kind="stable").reset_index(drop=True), B.sort_values("_k", kind="stable").reset_index(drop=True)
    res = {"rows_a": int(len(A)), "rows_b": int(len(B)), "sha_a": lb.sha256(a_path), "sha_b": lb.sha256(b_path), "sha_equal": lb.sha256(a_path) == lb.sha256(b_path), "cols": {}}
    ka, kb = set(A._k), set(B._k)
    res["only_a"], res["only_b"] = int(len(ka - kb)), int(len(kb - ka))
    common = sorted(ka & kb)
    Ai, Bi = A.set_index("_k").loc[common], B.set_index("_k").loc[common]
    rows = []
    for c in lb.LEDGER_COLUMNS:
        ne = ~li._eq(Ai[c], Bi[c])
        res["cols"][c] = int(ne.sum())
        for k in np.asarray(common)[ne][:200]:
            rows.append({"key": k, "field": c, "incremental": li._s(Ai.at[k, c]), "full": li._s(Bi.at[k, c])})
    for k in sorted(ka - kb)[:200]:
        rows.append({"key": k, "field": "row", "incremental": "present", "full": "absent"})
    for k in sorted(kb - ka)[:200]:
        rows.append({"key": k, "field": "row", "incremental": "absent", "full": "present"})
    pd.DataFrame(rows, columns=["key", "field", "incremental", "full"]).to_csv(out_csv, index=False)
    res["cells_differ"] = int(sum(res["cols"].values()))
    res["identical"] = res["cells_differ"] == 0 and res["only_a"] == 0 and res["only_b"] == 0 and res["rows_a"] == res["rows_b"]
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=Path("/home/jgo/longshort/daily_D1"))
    ap.add_argument("--baseline", type=Path, default=L2 / "R1")
    ap.add_argument("--extract", type=Path, default=None, help="기본 <out>/extract (없으면 xfeed 에서 추출)")
    ap.add_argument("--start", default="2026-09-10")
    ap.add_argument("--dates", default="2026-09-11,2026-09-14,2026-09-15")
    ap.add_argument("--skip-chain", action="store_true")
    ap.add_argument("--skip-full", action="store_true")
    ap.add_argument("--full-mode", choices=("refresh", "scratch", "both"), default="refresh")
    ap.add_argument("--jobs", type=int, default=3)
    a = ap.parse_args(argv)
    out = a.out
    extract = a.extract or (out / "extract")
    dates = [d.strip() for d in a.dates.split(",") if d.strip()]
    timings = {}
    if not (extract / "raw_ciqdividend.parquet").exists():
        ids = pd.read_parquet(a.baseline / "root" / "master" / "instruments.parquet", columns=["tradingItemId"]).tradingItemId
        li.extract_sources(ids, extract, a.start, dates[-1])
    # chain (sequential)
    chain = out / "chain"
    if not a.skip_chain:
        prev = a.baseline
        for i, d in enumerate([a.start] + dates):
            tag = "s" + d.replace("-", "")[4:]
            stage = chain / tag
            if stage.exists() and (stage / li.STAGE_INFO).exists() and i == 0:
                prev = stage
                continue
            rc, secs = chain_step(prev, d, stage, extract, bootstrap=(i == 0), log=chain / f"{tag}.log", calendar=a.baseline / "root" / "calendar.parquet")
            timings[f"chain:{d}"] = {"rc": rc, "seconds": round(secs, 1)}
            if rc:
                print(f"chain step {d} failed (rc {rc}); see {chain / (tag + '.log')}")
                return 1
            prev = stage
    # full rebuilds (parallel)
    modes = ["refresh", "scratch"] if a.full_mode == "both" else [a.full_mode]
    if not a.skip_full:
        jobs = [(d, m) for d in dates for m in modes]
        with ThreadPoolExecutor(max_workers=a.jobs) as ex:
            futs = {ex.submit(full_build, a.baseline, extract, d, out / "full" / f"{m}_{d}", m, out / "full" / f"{m}_{d}.log"): (d, m) for d, m in jobs}
            for f, (d, m) in futs.items():
                rc, secs = f.result()
                timings[f"full:{m}:{d}"] = {"rc": rc, "seconds": round(secs, 1)}
                if rc:
                    print(f"full build {m} {d} failed (rc {rc})")
    # compare
    results = []
    for d in dates:
        tag = "s" + d.replace("-", "")[4:]
        inc = chain / tag / "events" / "ca_ledger.parquet"
        for m in modes:
            full = out / "full" / f"{m}_{d}" / "events" / "ca_ledger.parquet"
            if not inc.exists() or not full.exists():
                results.append({"date": d, "full_mode": m, "status": "missing"})
                continue
            r = compare(inc, full, out / f"diff_{m}_{d}.csv")
            info = json.loads((chain / tag / li.STAGE_INFO).read_text())
            r.update({"date": d, "full_mode": m, "chain_seconds": info["seconds"]["total"], "chain_rss_gb": info["peak_rss_gb"], "revisions": info["revisions"],
                      "qa_all_ok": (info.get("qa") or {}).get("all_ok")})
            results.append(r)
    (out / "gate_results.json").write_text(json.dumps({"results": results, "timings": timings, "at": time.strftime("%Y-%m-%d %H:%M:%S")}, indent=1, default=str))
    lines = ["| date | full mode | rows inc / full | sha equal | cells differ | only inc / only full | identical | revisions (+/−/~) | chain s / RSS GB | QA all_ok |", "|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        if r.get("status") == "missing":
            lines.append(f"| {r['date']} | {r['full_mode']} | missing | | | | | | | |")
            continue
        rv = r["revisions"]
        lines.append(f"| {r['date']} | {r['full_mode']} | {r['rows_a']} / {r['rows_b']} | {r['sha_equal']} | {r['cells_differ']} | {r['only_a']} / {r['only_b']} | **{r['identical']}** | "
                     f"+{rv['added']} / −{rv['removed']} / ~{rv['changed_rows']} | {r['chain_seconds']} / {r['chain_rss_gb']} | {r['qa_all_ok']} |")
    (out / "gate_table.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(json.dumps(timings, indent=1))
    return 0 if all(r.get("identical") for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
