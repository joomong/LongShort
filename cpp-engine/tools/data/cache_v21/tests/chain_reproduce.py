#!/home/jgo/usr/venv-3.11/bin/python
"""cache_v21/tests/chain_reproduce.py - D3: daily chain reproduction harness (rc2f3 published copy -> N trading days of
`update_daily` -> last-asOf full regeneration references -> file-by-file comparison with attribution of every differing cell).

Layout (--root, default /home/jgo/longshort/daily/D3):
  prev_work/            T1/_work files lent to the first build from the published copy (--prev-work)
  b1_<d1>/ b2_<d2>/ ... chain stage builds (update_daily --out-stage), pub_<d>/ their publish dry-runs (--publish-out, no --remote)
  one_<dN>/ pub_one_<dN>/  ONE-STEP reference: rc2f3 -> dN in a single update_daily run with --full-lifecycle (every
                        daily-regenerated layer - factors, lifecycle aggregates, universe, events, ledger, returns, exports - is
                        produced once from the sources as loaded at run time; no intermediate build) = path-independence + full
                        lifecycle aggregation reference
  full_fetch/           FULL-FETCH reference: prices_build.py --ids-file (chain-end instruments + aux ids) --asof dN = complete
                        vendor re-pull of ciqpriceequity through the T2 transform (raw = ciq x adjf) -> base_data_full.parquet +
                        adjf_long.parquet (adjustment_factor_daily); the chain only re-fetches the T-5..T-1 window + restated ids
  ledger_full_<dN>/     LEDGER reference: ledger_build.py --incremental --prev <rc2f R1 stage> --asof dN --build <chain end> =
                        one full source re-query + full ledger recomputation on the chain end's own inputs (D1 'one-step')
  probes/               xfl_control load-state snapshots before/after every run (vendor load timing evidence)
  compare/              per-file cell diffs (csv) + attribution tables (csv/md) + summary.json

Steps (--steps, default all; every step is resumable through <root>/state/<step>.done):
  chain      run update_daily for each --days date in order (first: --prev-build <published copy> --prev-work --ledger-prev)
  onestep    one-step reference build for the last day (--full-lifecycle) [runs concurrently with `chain` under --parallel]
  fullfetch  prices_build --ids-file for the chain-end id set (waits for the first chain build's instruments)
  ledgerfull ledger one-step on the chain end's inputs
  day0       same-asOf re-run of the snapshot (update_daily --asof <snapshot asOf>) -> compared with the snapshot = transition effects
  compare    comparisons + attribution -> compare/ ; exit 1 when any differing cell is unattributed
  report     tables copied to --tables-out (small files)

Rules: 61 published build / rc2f stages / D1 / D2 outputs are read only (no --remote), xfeed read-only (chunks <= 1000 through the
modules), pyarrow threads <= 8, nothing is committed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CV = HERE.parent
sys.path.insert(0, str(CV))
os.environ.setdefault("OMP_NUM_THREADS", "8")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

pa.set_cpu_count(8)
pa.set_io_thread_count(4)

PY = "/home/jgo/usr/venv-3.11/bin/python"
DEFAULT_ROOT = Path("/home/jgo/longshort/daily/D3")                  # dev run root (created on demand)
DEFAULT_START = Path("/home/jgo/61/data2/temp_stock/cache_v2.1_cash_1d")   # 61 rc2f3 published build (read only)
DEFAULT_LEDGER_PREV = Path("/home/jgo/longshort/clean_t4000/C4/final4/R1")   # latest rc2f apply R1 stage (hand-over files for the ledger builder)
DEFAULT_ORCH = Path("/home/jgo/longshort/daily/orch")
DEFAULT_PREV_WORK_SRC = Path("/home/jgo/longshort/rebuild_deps/stage_v21/T1/_work")
PREV_WORK_FILES = ("calendar", "lineage_overlap", "identity", "ciq_agg", "mi_agg", "candidate_negatives")
DEFAULT_DAYS = ("2026-09-16", "2026-09-17", "2026-09-18")
KST = dt.timezone(dt.timedelta(hours=9))


def now_str() -> str:
    return dt.datetime.now(tz=KST).strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str, root: Path | None = None):
    line = f"[{now_str()}] {msg}"
    print(line, flush=True)
    if root is not None:
        with open(root / "logs" / "chain_reproduce.log", "a") as fh:
            fh.write(line + "\n")


def done_path(root: Path, step: str) -> Path:
    return root / "state" / f"{step}.done"


def mark_done(root: Path, step: str, info: dict | None = None):
    done_path(root, step).parent.mkdir(parents=True, exist_ok=True)
    done_path(root, step).write_text(json.dumps({"at": now_str(), **(info or {})}, indent=1, default=str))


def lock_path(root: Path, step: str) -> Path:
    return root / "state" / f"{step}.running"


def running_elsewhere(root: Path, step: str) -> bool:
    """True when another harness process holds the step's lock (pid alive) -> never delete/restart a build in progress."""
    p = lock_path(root, step)
    if not p.exists():
        return False
    try:
        pid = int(json.loads(p.read_text()).get("pid", 0))
        os.kill(pid, 0)
        return pid != os.getpid()
    except (ValueError, ProcessLookupError, PermissionError):
        return False


class StepLock:
    def __init__(self, root: Path, step: str):
        self.p = lock_path(root, step)

    def __enter__(self):
        self.p.parent.mkdir(parents=True, exist_ok=True)
        self.p.write_text(json.dumps({"pid": os.getpid(), "at": now_str()}))
        return self

    def __exit__(self, *exc):
        self.p.unlink(missing_ok=True)


def run_logged(cmd: list[str], log_path: Path, root: Path, cwd: Path | None = None) -> dict:
    """Run a subprocess, capture stdout+stderr to log_path, measure wall time and the child's peak RSS (resource usage)."""
    import resource
    log(f"START {log_path.stem}: {' '.join(cmd)}", root)
    t0 = time.time()
    r0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    with open(log_path, "w") as fh:
        rc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, cwd=str(cwd or CV)).returncode
    r1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    info = {"rc": rc, "seconds": round(time.time() - t0, 1), "children_maxrss_gb": round(r1.ru_maxrss / 1024 / 1024, 2),
            "log": str(log_path), "cmd": cmd}
    log(f"END {log_path.stem}: rc={rc} {info['seconds']}s", root)
    return info


# --------------------------------------------------------------------------------------
# xfl_control probe (vendor load timing evidence; read-only, small tables only)
# --------------------------------------------------------------------------------------
PROBE_TABLES = ("ciqPriceEquity", "ciqPriceEquityDivAdjFactor", "MIPrice", "ciqSplit", "ciqSplitCache", "ciqDividend", "ciqDividendCache",
                "ciqTradingItem", "ciqSecurity", "ciqExchangeRate", "ciqMarketCap", "ciqGvkeyIID", "ciqCurrency")


def probe(root: Path, label: str) -> dict:
    """Snapshot of the loader control table for the tables the build reads (last zip, load end time, result)."""
    out = {"label": label, "at": now_str()}
    try:
        from db import connect
        conn = connect(statement_timeout_ms=20000, application_name="cache_v21_d3_probe")
        sql = """select t.tablename, s.loadsubid, s.enddatetime, s.lastresulttypeid, z.actualzipfilename as lastzip, z.lastdownloadosdatetime as lastzip_dl
                 from xfl_control.xfu_loadsubtablestate s
                 join xfl_control.xfc_tableinst ti using(tableinstid) join xfl_control.xfc_table t using(tableid)
                 left join xfl_control.xfu_actualzipfile z on z.actualzipfileid = s.lastactualzipfileid
                 where t.tablename = any(%s) and s.enddatetime is not null"""
        with conn.cursor() as cur:
            cur.execute(sql, (list(PROBE_TABLES),))
            rows = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
        conn.close()
        for r in rows:
            for k in ("enddatetime", "lastzip_dl"):
                if r.get(k) is not None:
                    r[k] = str(r[k])
        out["rows"] = sorted(rows, key=lambda r: (r["tablename"], r["enddatetime"]))
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    (root / "probes").mkdir(parents=True, exist_ok=True)
    p = root / "probes" / f"xfl_control_{label}.json"
    p.write_text(json.dumps(out, indent=1, default=str))
    return out


# --------------------------------------------------------------------------------------
# run steps
# --------------------------------------------------------------------------------------
def update_daily_cmd(prev: Path, out: Path, pub: Path, asof: str, root: Path, args, first: bool, extra: list[str] | None = None) -> list[str]:
    cmd = [PY, str(CV / "update_daily.py"), "--prev-build", str(prev), "--out-stage", str(out), "--publish-out", str(pub), "--asof", asof,
           "--work-root", str(root), "--orch", str(args.orch), "--threads", str(args.threads)]
    if first:
        cmd += ["--prev-work", str(root / "prev_work"), "--ledger-prev", str(args.ledger_prev)]
    return cmd + (extra or [])


def step_chain(root: Path, args) -> dict:
    res = {}
    prev = Path(args.start)
    for i, d in enumerate(args.days):
        tag = d[5:].replace("-", "")
        out, pub = root / f"b{i + 1}_{tag}", root / f"pub_{tag}"
        marker = done_path(root, f"chain_{tag}")
        if marker.exists() and (out / "BUILD.json").exists():
            res[d] = json.loads(marker.read_text())
            prev = out
            continue
        if running_elsewhere(root, f"chain_{tag}"):
            raise SystemExit(f"chain build {d} is running in another harness process (state/chain_{tag}.running)")
        with StepLock(root, f"chain_{tag}"):
            for p in (out, pub):
                if p.exists():
                    shutil.rmtree(p)
            probe(root, f"before_chain_{tag}")
            info = run_logged(update_daily_cmd(prev, out, pub, d, root, args, first=(i == 0)), root / "logs" / f"chain_{tag}.log", root)
            probe(root, f"after_chain_{tag}")
        if info["rc"] not in (0, args.exposure_warn_rc):
            raise SystemExit(f"chain build {d} failed rc={info['rc']} (see {info['log']})")
        mark_done(root, f"chain_{tag}", info)
        res[d] = info
        prev = out
    mark_done(root, "chain", {"days": list(args.days)})
    return res


def step_onestep(root: Path, args, day: str | None = None) -> dict:
    day = day or args.days[-1]
    tag = day[5:].replace("-", "")
    out, pub = root / f"one_{tag}", root / f"pub_one_{tag}"
    marker = done_path(root, f"onestep_{tag}")
    if marker.exists() and (out / "BUILD.json").exists():
        return json.loads(marker.read_text())
    if running_elsewhere(root, f"onestep_{tag}"):
        raise SystemExit(f"one-step build {day} is running in another harness process (state/onestep_{tag}.running)")
    with StepLock(root, f"onestep_{tag}"):
        for p in (out, pub):
            if p.exists():
                shutil.rmtree(p)
        probe(root, f"before_onestep_{tag}")
        extra = ["--full-lifecycle"] if (day == args.days[-1] and not args.no_full_lifecycle) else []
        info = run_logged(update_daily_cmd(Path(args.start), out, pub, day, root, args, first=True, extra=extra), root / "logs" / f"onestep_{tag}.log", root)
        probe(root, f"after_onestep_{tag}")
    if info["rc"] not in (0, args.exposure_warn_rc):
        raise SystemExit(f"one-step build {day} failed rc={info['rc']} (see {info['log']})")
    mark_done(root, f"onestep_{tag}", info)
    return info


def step_day0(root: Path, args) -> dict:
    """Same-asOf re-run of the snapshot: update_daily --asof <snapshot asOf> from the published copy (allowed: 'same asOf re-run'), published
    dry-run -> compared with the snapshot itself = the daily path vs the snapshot path at the SAME asOf (the transition effects list)."""
    start = Path(args.start)
    asof = json.loads((start / "manifest.json").read_text()).get("asOfDate")
    tag = asof[5:].replace("-", "")
    out, pub = root / f"day0_{tag}", root / f"pub_day0_{tag}"
    marker = done_path(root, f"day0_{tag}")
    if marker.exists() and (out / "BUILD.json").exists():
        return json.loads(marker.read_text())
    if running_elsewhere(root, f"day0_{tag}"):
        raise SystemExit("day0 build is running in another harness process")
    with StepLock(root, f"day0_{tag}"):
        for p in (out, pub):
            if p.exists():
                shutil.rmtree(p)
        probe(root, f"before_day0_{tag}")
        info = run_logged(update_daily_cmd(start, out, pub, asof, root, args, first=True), root / "logs" / f"day0_{tag}.log", root)
        probe(root, f"after_day0_{tag}")
        if info["rc"] not in (0, args.exposure_warn_rc):
            raise SystemExit(f"day0 build {asof} failed rc={info['rc']} (see {info['log']})")
        info["asOf"] = asof
        mark_done(root, f"day0_{tag}", info)
    return info


def chain_end(root: Path, args) -> Path:
    return root / f"b{len(args.days)}_{args.days[-1][5:].replace('-', '')}"


def step_fullfetch(root: Path, args) -> dict:
    """Full vendor re-pull of ciqpriceequity for the chain-end id set (instruments + aux) at the last asOf."""
    marker = done_path(root, "fullfetch")
    ff = root / "full_fetch"
    if marker.exists() and (ff / "base_data_full.parquet").exists():
        return json.loads(marker.read_text())
    # id set: first chain build's instruments (new ids of the day are added in U1) + aux ids of the start build; the compare step
    # verifies the chain-end set and fetches any id that is still missing
    first = root / f"b1_{args.days[0][5:].replace('-', '')}"
    inst = first / "T1" / "master" / "instruments.parquet"
    chain_log = root / "logs" / f"chain_{args.days[0][5:].replace('-', '')}.log"
    waited = 0

    def u1_done() -> bool:      # prepare copies the previous instruments first; U1 rewrites it with the day's new ids -> wait for "== U1 done"
        if (first / "BUILD.json").exists():
            return True
        return chain_log.exists() and "== U1 done" in chain_log.read_text(errors="replace")
    while not (inst.exists() and u1_done()) and waited < 3600:
        time.sleep(15)
        waited += 15
    if not (inst.exists() and u1_done()):
        raise SystemExit(f"fullfetch: {inst} / U1 of the first chain build not available after {waited}s")
    ids = set(pd.read_parquet(inst, columns=["tradingItemId"]).tradingItemId.astype(int))
    start = Path(args.start)
    aux = pd.read_parquet(start / "master" / "aux_instruments.parquet")
    ids |= set(int(i) for i in aux.loc[aux.priceFetch.astype(str) == "usd", "tradingItemId"]) if "priceFetch" in aux.columns else set(int(i) for i in aux.tradingItemId)
    lc = pd.read_parquet(start / "master" / "lifecycle.parquet", columns=["tradingItemId", "status"])
    ids |= set(int(i) for i in lc.loc[lc.status == "aux", "tradingItemId"])
    if running_elsewhere(root, "fullfetch"):
        raise SystemExit("fullfetch is running in another harness process")
    with StepLock(root, "fullfetch"):
        ff.mkdir(parents=True, exist_ok=True)
        (ff / "work").mkdir(exist_ok=True)
        pd.DataFrame({"tradingItemId": sorted(ids)}).to_parquet(ff / "ids.parquet", index=False)
        probe(root, "before_fullfetch")
        cmd = [PY, str(CV / "prices_build.py"), "--stage", str(ff), "--work", str(ff / "work"), "--ids-file", str(ff / "ids.parquet"),
               "--out-name", "base_data_full.parquet", "--asof", args.days[-1], "--threads", str(args.threads)]
        info = run_logged(cmd, root / "logs" / "fullfetch.log", root)
        probe(root, "after_fullfetch")
    if info["rc"] != 0:
        raise SystemExit(f"fullfetch failed rc={info['rc']}")
    info["ids"] = len(ids)
    mark_done(root, "fullfetch", info)
    return info


def step_fullfetch_extra(root: Path, args) -> dict:
    """Ids present in the chain end's base_data but missing from full_fetch/ids.parquet (new ids added by U1 after the fetch started)."""
    ff = root / "full_fetch"
    marker = done_path(root, "fullfetch_extra")
    if marker.exists():
        return json.loads(marker.read_text())
    b = chain_end(root, args)
    pub = root / f"pub_{args.days[-1][5:].replace('-', '')}"
    src = pub / "base_data.parquet" if (pub / "base_data.parquet").exists() else b / "T2" / "base_data.parquet"
    have = set(pd.read_parquet(ff / "ids.parquet").tradingItemId.astype(int))
    ids = set(pq.read_table(src, columns=["tradingItemId"]).column(0).unique().to_pylist()) - have
    info = {"missing_ids": sorted(ids)}
    if ids:
        with StepLock(root, "fullfetch_extra"):
            pd.DataFrame({"tradingItemId": sorted(ids)}).to_parquet(ff / "ids_extra.parquet", index=False)
            (ff / "work_extra").mkdir(exist_ok=True)
            cmd = [PY, str(CV / "prices_build.py"), "--stage", str(ff), "--work", str(ff / "work_extra"), "--ids-file", str(ff / "ids_extra.parquet"),
                   "--out-name", "base_data_full_extra.parquet", "--asof", args.days[-1], "--threads", str(args.threads)]
            probe(root, "before_fullfetch_extra")
            r = run_logged(cmd, root / "logs" / "fullfetch_extra.log", root)
            probe(root, "after_fullfetch_extra")
            if r["rc"] != 0:
                raise SystemExit("fullfetch_extra failed")
            info.update(r)
    mark_done(root, "fullfetch_extra", info)
    return info


def step_ledgerfull(root: Path, args) -> dict:
    """One full source re-query + full ledger build from the rc2f R1 stage on the chain end's own inputs (D1 one-step)."""
    day = args.days[-1]
    tag = day[5:].replace("-", "")
    marker = done_path(root, "ledgerfull")
    out = root / f"ledger_full_{tag}"
    if marker.exists() and (out / "events" / "ca_ledger.parquet").exists():
        return json.loads(marker.read_text())
    if running_elsewhere(root, "ledgerfull"):
        raise SystemExit("ledgerfull is running in another harness process")
    if out.exists():
        shutil.rmtree(out)
    b = chain_end(root, args)
    # same call as update_daily_rc2f.run_ledger_incremental (D2), with --prev = the rc2f R1 stage instead of the previous day's T6
    cmd = [PY, str(CV / "ledger_build.py"), "--incremental", "--prev", str(args.ledger_prev), "--asof", day, "--out", str(out), "--build", str(b),
           "--overrides-glob", args.overrides_glob, "--threads", str(args.threads)]
    with StepLock(root, "ledgerfull"):
        probe(root, "before_ledgerfull")
        info = run_logged(cmd, root / "logs" / "ledgerfull.log", root)
        probe(root, "after_ledgerfull")
    if info["rc"] != 0:
        raise SystemExit(f"ledgerfull failed rc={info['rc']}")
    mark_done(root, "ledgerfull", info)
    return info


# --------------------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    ap.add_argument("--start", type=Path, default=DEFAULT_START, help="published build copy used as the chain start (read only)")
    ap.add_argument("--days", nargs="+", default=list(DEFAULT_DAYS), help="trading days of the chain in order (asOf dates)")
    ap.add_argument("--steps", default="chain,onestep,fullfetch,day0,fullfetch_extra,ledgerfull,compare,report")
    ap.add_argument("--parallel", action="store_true", help="run onestep + fullfetch concurrently with the chain (3 DB clients)")
    ap.add_argument("--onestep-days", nargs="*", default=None, help="one-step reference days to build (default: the last chain day; the last day gets --full-lifecycle)")
    ap.add_argument("--no-full-lifecycle", action="store_true", help="one-step reference without --full-lifecycle")
    ap.add_argument("--ledger-prev", type=Path, default=DEFAULT_LEDGER_PREV)
    ap.add_argument("--orch", type=Path, default=DEFAULT_ORCH)
    ap.add_argument("--prev-work-src", type=Path, default=DEFAULT_PREV_WORK_SRC)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--overrides-glob", default=str(CV / "overrides" / "rc2f" / "*" / "*.csv"))
    ap.add_argument("--semantic-gate", choices=["fail", "warn"], default="fail", help="value-level checks on the chain end vs the snapshot (new historical |ret|>5 cells, duplicated child legs on linked parents): fail = exit 1")
    ap.add_argument("--exposure-warn-rc", type=int, default=9)
    ap.add_argument("--tables-out", type=Path, default=Path("/home/jgo/longshort/rebuild_deps/stage_daily/D3"))
    args = ap.parse_args(argv)
    root = args.root
    for d in ("logs", "state", "probes", "compare", "prev_work"):
        (root / d).mkdir(parents=True, exist_ok=True)
    for name in PREV_WORK_FILES:
        src, dst = args.prev_work_src / f"{name}.parquet", root / "prev_work" / f"{name}.parquet"
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)
    steps = [s for s in args.steps.split(",") if s]
    log(f"chain_reproduce start root={root} start={args.start} days={args.days} steps={steps} parallel={args.parallel}", root)
    procs = {}
    if args.parallel and ("onestep" in steps or "fullfetch" in steps) and "chain" in steps and not done_path(root, "chain").exists():
        # spawn the reference builds as separate harness processes so they overlap the chain (each is resumable/idempotent)
        for s in ("onestep", "fullfetch"):
            if s in steps and not done_path(root, s if s == "fullfetch" else f"onestep_{args.days[-1][5:].replace('-', '')}").exists():
                sub_steps = s
                if s == "fullfetch" and "day0" in steps:          # the day0 rerun follows the full fetch in the same process (3 DB clients at most)
                    sub_steps = "fullfetch,day0"
                    steps = [x for x in steps if x != "day0"]
                sub = [PY, str(Path(__file__).resolve()), "--root", str(root), "--start", str(args.start), "--days", *args.days, "--steps", sub_steps,
                       "--ledger-prev", str(args.ledger_prev), "--orch", str(args.orch), "--threads", str(args.threads)]
                if s == "onestep":
                    sub += ["--onestep-days", *(args.onestep_days or [args.days[-1]])]
                fh = open(root / "logs" / f"harness_{s}.log", "w")
                procs[s] = (subprocess.Popen(sub, stdout=fh, stderr=subprocess.STDOUT, cwd=str(CV)), fh)
                log(f"spawned {s} ({sub_steps}, pid {procs[s][0].pid})", root)
                steps = [x for x in steps if x != s]
    results = {}
    if "chain" in steps:
        results["chain"] = step_chain(root, args)
    if "onestep" in steps:
        for d in (args.onestep_days or [args.days[-1]]):
            results[f"onestep_{d}"] = step_onestep(root, args, day=d)
    if "fullfetch" in steps:
        results["fullfetch"] = step_fullfetch(root, args)
    for s, (p, fh) in procs.items():
        rc = p.wait()
        fh.close()
        log(f"{s} process finished rc={rc}", root)
        if rc != 0:
            raise SystemExit(f"{s} failed (see logs/harness_{s}.log)")
    if "day0" in steps:
        results["day0"] = step_day0(root, args)
    if "fullfetch" in steps or "fullfetch_extra" in steps:
        if done_path(root, "fullfetch").exists() and done_path(root, "chain").exists():
            results["fullfetch_extra"] = step_fullfetch_extra(root, args)
    if "ledgerfull" in steps:
        results["ledgerfull"] = step_ledgerfull(root, args)
    if "compare" in steps:
        from chain_compare import run_compare  # noqa: E402  (same directory)
        results["compare"] = run_compare(root, args)
        rc = 0
        if results["compare"].get("unattributed_total", 0):
            log(f"FAIL: unattributed cells {results['compare']['unattributed_total']}", root)
            rc = 1
        if results["compare"].get("semantic_fail", {}).get("fail"):
            log(f"{'FAIL' if args.semantic_gate == 'fail' else 'WARN'}: semantic gate {results['compare']['semantic_fail']}", root)
            if args.semantic_gate == "fail":
                rc = 1
        if rc:
            if "report" in steps:
                from chain_compare import write_report  # noqa: E402
                write_report(root, args)
            return rc
    if "report" in steps:
        from chain_compare import write_report  # noqa: E402
        write_report(root, args)
    log("chain_reproduce done rc=0", root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
