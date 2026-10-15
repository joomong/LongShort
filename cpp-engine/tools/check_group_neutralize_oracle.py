#!/usr/bin/env python3
"""Check <neutralize group="..."/> against the team sim implementation on real data.

Starts its own longshort-server on a free port, asks for raw signals through
/api/simulate (collectSignals) and compares them with
`func_util.lib_funcs.cs_group_neut`, the function production alphas use.

Primary check (isolates the module):
    A = <data expr="returns" delay="0"/><rank/>
    B = A followed by <neutralize group="..."/>
    B  ==  cs_group_neut(A, codes)     # A taken from the engine, codes from the same parquet
The group mean is a cross-sectional statistic, so A is requested over *every*
panel symbol; the pass/fail comparison is then reported both on a fixed ~60
symbol subset (requested separately, exactly as the engine would serve a plot)
and on the whole panel. Nothing is subsampled: the script refuses any payload
whose row stride is not 1 or whose symbol list is shorter than requested.

Secondary check (report only): the whole team chain
    cs_group_neut(cs_rank(returns[valid]), codes)
recomputed from the price cache. The engine's rank is r/n and the team's is
(r-1)/(n-1), so this chain is an affine rescaling of the engine signal; both the
raw and the scale-adjusted numbers are reported.

Exit code 0 when every group passes, 1 otherwise.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

REPO = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO / "cpp-engine/examples/datasets/equities_monthly_afl.yaml"
DEFAULT_GROUPS = REPO / "data/groups/cache_v1_cash_1d"
DEFAULT_CACHE = Path("/home/base/cache/cache_v1_cash_1d")
TEAM_SIM_UTILS = "/home/base/sim/utils"
INDEX_COLUMN = "__index_level_0__"

XML_A = '<data expr="returns" delay="0"/><rank/>'
XML_B = '<data expr="returns" delay="0"/><rank/><neutralize group="{group}"/>'

DIFF_TOLERANCE = 1e-5
GROUP_SUM_TOLERANCE = 1e-4


# --------------------------------------------------------------------------- server


class Server:
    """One longshort-server started by this script and stopped by this script."""

    def __init__(self, binary: Path, config: Path, port: int, log: Path):
        self.binary, self.config, self.port, self.log = binary, config, port, log
        self.process = None

    def __enter__(self):
        env = dict(os.environ, LONGSHORT_PORT=str(self.port))
        self.handle = self.log.open("w")
        self.process = subprocess.Popen([str(self.binary), str(self.config)],
                                        stdout=self.handle, stderr=subprocess.STDOUT, env=env)
        deadline = time.time() + 900
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"server exited with {self.process.returncode}; see {self.log}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/health", timeout=5) as r:
                    if json.loads(r.read()).get("status") == "ok":
                        return self
            except (urllib.error.URLError, ConnectionError, TimeoutError, json.JSONDecodeError):
                pass
            time.sleep(1.0)
        raise RuntimeError(f"server did not become ready on port {self.port}; see {self.log}")

    def __exit__(self, *exc):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.handle.close()
        return False

    def peak_rss_kb(self):
        if self.process is None or self.process.poll() is not None:
            return None
        for line in Path(f"/proc/{self.process.pid}/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1])
        return None

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=1800) as r:
            return json.loads(r.read())

    def post(self, path, payload):
        data = json.dumps(payload).encode()
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data,
                                         headers={"Content-Type": "application/json"})
        started = time.time()
        with urllib.request.urlopen(request, timeout=7200) as r:
            body = json.loads(r.read())
        return body, time.time() - started


# --------------------------------------------------------------------------- payloads


def request_signal(server, xml, symbols, is_start, os_start):
    """Raw signal matrix for one alpha over exactly `symbols`, every trading bar."""
    payload = {"xml": xml, "isStart": is_start, "osStart": os_start, "collectSignals": True,
               "signalPlotSymbols": list(symbols), "signalPlotMaxCells": 8000000}
    body, seconds = server.post("/api/simulate", payload)
    if len(body.get("batches", [])) != 1:
        raise RuntimeError(f"expected a single alpha for {xml!r}")
    plot = body["signalPlot"]
    if list(plot["symbols"]) != list(symbols):
        raise RuntimeError("the server returned a different symbol set than requested")
    if int(plot["rowStride"]) != 1 or len(plot["timestamps"]) != int(plot["originalRows"]):
        raise RuntimeError(f"rows were subsampled: stride={plot['rowStride']} "
                           f"rows={len(plot['timestamps'])}/{plot['originalRows']}")
    values = np.array(plot["values"], dtype=np.float64)  # JSON null -> NaN
    if values.shape != (len(plot["timestamps"]), len(symbols)):
        raise RuntimeError(f"unexpected signal shape {values.shape}")
    return pd.DatetimeIndex(plot["timestamps"]), values, seconds


def read_wide(path, symbols, index):
    """Wide panel restricted to `symbols` (absent columns become NaN) and to `index`."""
    names = set(pq.ParquetFile(path).schema_arrow.names)
    present = [s for s in symbols if s in names]
    table = pq.read_table(path, columns=[INDEX_COLUMN] + present)
    frame = table.to_pandas()
    if INDEX_COLUMN in frame.columns:  # pandas metadata may already restore it as the index
        frame = frame.set_index(INDEX_COLUMN)
    frame.index = pd.DatetimeIndex(frame.index)
    # The base cache restores integer column labels while the group panels restore
    # strings; the engine only ever sees the physical (string) parquet names.
    frame.columns = frame.columns.astype(str)
    missing = [s for s in symbols if s not in names]
    return frame.reindex(index=index).reindex(columns=list(symbols)), missing


# --------------------------------------------------------------------------- statistics


def compare(engine, reference, tolerance=DIFF_TOLERANCE):
    engine_nan, reference_nan = ~np.isfinite(engine), ~np.isfinite(reference)
    both = ~engine_nan & ~reference_nan
    difference = np.where(both, np.abs(engine - reference), 0.0)
    matched = (engine_nan & reference_nan) | (both & (difference <= tolerance))
    return {"cells": int(engine.size),
            "max_abs_diff": float(difference.max()) if engine.size else 0.0,
            "match_share": float(matched.mean()) if engine.size else 1.0,
            "nan_agreement": float((engine_nan == reference_nan).mean()) if engine.size else 1.0,
            "both_finite": int(both.sum())}


def max_group_sum(signal, codes):
    """Largest |sum of the neutralized signal| over one date and one group."""
    worst, worst_date = 0.0, None
    integer_codes = np.where(np.isfinite(codes) & (codes >= 0), codes, -1).astype(np.int64)
    finite = np.isfinite(signal)
    width = int(integer_codes.max()) + 1 if integer_codes.size else 1
    for row in range(signal.shape[0]):
        keep = finite[row] & (integer_codes[row] >= 0)
        if not keep.any():
            continue
        sums = np.bincount(integer_codes[row][keep], weights=signal[row][keep], minlength=width)
        peak = float(np.abs(sums).max())
        if peak > worst:
            worst, worst_date = peak, row
    return worst, worst_date


# --------------------------------------------------------------------------- report


def table(rows, header):
    widths = [max(len(str(r[i])) for r in [header] + rows) for i in range(len(header))]
    lines = ["| " + " | ".join(str(h).ljust(w) for h, w in zip(header, widths)) + " |",
             "|" + "|".join("-" * (w + 2) for w in widths) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(c).ljust(w) for c, w in zip(row, widths)) + " |")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--build-dir", type=Path, default=Path("/tmp/longshort_build_final"))
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--groups", type=Path, default=DEFAULT_GROUPS)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--universe", type=Path,
                        default=DEFAULT_CACHE / "TopUniverseV1/Top500.parquet")
    parser.add_argument("--port", type=int, default=50117, help="never 50007 (shared server)")
    parser.add_argument("--subset-size", type=int, default=60)
    parser.add_argument("--is-start", default="2010-01-01")
    parser.add_argument("--os-start", default="2100-01-01")
    parser.add_argument("--report", type=Path,
                        default=Path("/tmp/group_neutralize_oracle_report.md"))
    parser.add_argument("--log-dir", type=Path, default=Path("/tmp"))
    parser.add_argument("--skip-secondary", action="store_true")
    args = parser.parse_args()
    if args.port == 50007:
        parser.error("port 50007 belongs to the shared server")

    sys.path.append(TEAM_SIM_UTILS)
    from func_util.lib_funcs import cs_group_neut, cs_rank  # noqa: E402

    binary = args.build_dir / "longshort-server"
    if not binary.is_file():
        parser.error(f"server binary not found: {binary}")
    log = args.log_dir / f"group_neutralize_oracle_{args.port}.log"

    notes, sections, failures = [], [], []
    with Server(binary, args.config.resolve(), args.port, log) as server:
        config = server.get("/api/config")
        symbols = list(config["symbols"])
        columns = set(config["columns"])
        for field in ("sector", "industry"):
            if field not in columns:
                raise RuntimeError(f"the panel does not publish '{field}'")
        step = max(1, len(symbols) // args.subset_size)
        subset = symbols[::step][:args.subset_size]

        dates, signal_a, seconds_a = request_signal(server, XML_A, symbols,
                                                    args.is_start, args.os_start)
        dates_sub, signal_a_sub, _ = request_signal(server, XML_A, subset,
                                                    args.is_start, args.os_start)
        subset_index = [symbols.index(s) for s in subset]
        subset_consistent = compare(signal_a_sub, signal_a[:, subset_index], tolerance=0.0)
        notes.append(f"Panel {len(dates)} bars x {len(symbols)} symbols "
                     f"({dates[0].date()}..{dates[-1].date()}); subset {len(subset)} symbols "
                     f"(every {step}th). A requested over the full panel in {seconds_a:.1f}s.")
        notes.append("A on the subset equals A on the full panel restricted to it: "
                     f"max|diff| {subset_consistent['max_abs_diff']:.3e}, "
                     f"NaN agreement {subset_consistent['nan_agreement']:.6f}.")

        frame_a = pd.DataFrame(signal_a, index=dates, columns=symbols)
        team_rank = None
        if not args.skip_secondary:
            # The whole team chain from the price cache: returns, universe mask, rank.
            close, _ = read_wide(args.cache / "BaseData/close.parquet", symbols, None)
            valid, _ = read_wide(args.universe, symbols, None)
            mask = valid.reindex_like(close).fillna(False).infer_objects(copy=False).astype(bool)
            masked = close.pct_change(fill_method=None).where(mask)
            team_rank = cs_rank(masked.loc[dates])
            del close, valid, mask, masked
            # Separate the rank stage from the neutralize stage: the engine ranks r/n
            # and the team (r-1)/(n-1), so team = (n*engine - 1)/(n - 1) exactly when
            # both stages see the same finite values in the same order.
            counts = np.isfinite(signal_a).sum(axis=1).astype(np.float64)[:, None]
            rank_stats = compare(np.where(counts > 1, (counts * signal_a - 1.0) /
                                          np.maximum(counts - 1.0, 1.0), signal_a),
                                 team_rank.to_numpy())
            notes.append(
                "Secondary rank stage alone (engine rank rewritten in the team convention) vs "
                f"`cs_rank(returns[valid])`: max|diff| {rank_stats['max_abs_diff']:.3e}, "
                f"match share {rank_stats['match_share']:.6f}, "
                f"NaN agreement {rank_stats['nan_agreement']:.6f} over "
                f"{rank_stats['both_finite']:,} finite cells. Any residual below is inherited "
                "from this stage, not from `neutralize`.")
        primary_rows, group_rows, secondary_rows = [], [], []
        for group in ("sector", "industry"):
            codes_frame, missing = read_wide(args.groups / f"{group}.parquet", symbols, dates)
            if missing:
                notes.append(f"{group}: {len(missing)} panel symbols are absent from the group "
                             f"panel and stay unclassified (e.g. {missing[:3]}).")
            codes = codes_frame.to_numpy(dtype=np.float64)
            reference = cs_group_neut(frame_a, codes_frame.fillna(-1).astype(int)).to_numpy()

            _, signal_b_sub, _ = request_signal(server, XML_B.format(group=group), subset,
                                                args.is_start, args.os_start)
            _, signal_b, seconds_b = request_signal(server, XML_B.format(group=group), symbols,
                                                    args.is_start, args.os_start)
            subset_stats = compare(signal_b_sub, reference[:, subset_index])
            full_stats = compare(signal_b, reference)
            peak, peak_row = max_group_sum(signal_b, codes)
            classified = int(np.isfinite(codes[0]).sum())
            group_count = int(np.nanmax(codes)) if np.isfinite(codes).any() else 0

            ok = (subset_stats["max_abs_diff"] < DIFF_TOLERANCE and
                  full_stats["max_abs_diff"] < DIFF_TOLERANCE and
                  subset_stats["nan_agreement"] == 1.0 and full_stats["nan_agreement"] == 1.0 and
                  peak < GROUP_SUM_TOLERANCE)
            if not ok:
                failures.append(group)
            for scope, stats in (("subset (%d sym)" % len(subset), subset_stats),
                                 ("full panel", full_stats)):
                primary_rows.append([group, scope, f"{stats['max_abs_diff']:.3e}",
                                     f"{stats['match_share']:.8f}", f"{stats['nan_agreement']:.8f}",
                                     f"{stats['both_finite']:,}", f"{stats['cells']:,}"])
            group_rows.append([group, group_count, classified, f"{peak:.3e}",
                               str(dates[peak_row].date()) if peak_row is not None else "-",
                               f"{seconds_b:.1f}s", "PASS" if ok else "FAIL"])

            if team_rank is not None:
                chain = cs_group_neut(team_rank, codes_frame.fillna(-1).astype(int)).to_numpy()
                raw = compare(signal_b, chain)
                # The engine ranks r/n and the team (r-1)/(n-1): an affine rescaling per date.
                counts = np.isfinite(signal_a).sum(axis=1).astype(np.float64)
                scale = np.where(counts > 1, counts / np.maximum(counts - 1.0, 1.0), 1.0)
                adjusted = compare(signal_b * scale[:, None], chain)
                secondary_rows.append([group, f"{raw['max_abs_diff']:.3e}",
                                       f"{raw['match_share']:.6f}",
                                       f"{adjusted['max_abs_diff']:.3e}",
                                       f"{adjusted['match_share']:.6f}",
                                       f"{raw['nan_agreement']:.6f}"])
                del chain
            del signal_b, signal_b_sub, reference, codes, codes_frame

        rss = server.peak_rss_kb()

    sections.append("## Primary — engine `neutralize` vs `cs_group_neut` on the engine's own A\n")
    sections.append(table(primary_rows, ["group", "scope", "max\\|diff\\|", "match share (1e-5)",
                                         "NaN agreement", "both finite", "cells"]))
    sections.append("\n## Group sums and verdict\n")
    sections.append(table(group_rows, ["group", "codes", "classified symbols",
                                       "max per-date group sum", "worst date",
                                       "full-panel request", "verdict"]))
    if secondary_rows:
        sections.append("\n## Secondary (report only) — full team chain "
                        "`cs_group_neut(cs_rank(returns[valid]), codes)`\n")
        sections.append(table(secondary_rows, ["group", "max\\|diff\\| raw", "match share raw",
                                               "max\\|diff\\| rank-scale adjusted",
                                               "match share adjusted", "NaN agreement"]))

    verdict = "PASS" if not failures else "FAIL (" + ", ".join(failures) + ")"
    header = [f"# `<neutralize>` real-data oracle — {verdict}", "",
              f"- config: `{args.config}`", f"- groups: `{args.groups}`",
              f"- server: `{binary}` on port {args.port}"
              + (f", peak RSS {rss:,} kB" if rss else ""),
              f"- pass criteria: primary max|diff| < {DIFF_TOLERANCE}, "
              f"per-date group sums < {GROUP_SUM_TOLERANCE}, NaN positions identical", ""]
    header += ["- " + note for note in notes] + [""]
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text("\n".join(header + sections) + "\n")
    print("\n".join(header + sections))
    print(f"\nreport: {args.report}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
