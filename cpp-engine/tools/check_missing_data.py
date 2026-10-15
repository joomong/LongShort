#!/usr/bin/env python3
"""Check wide-panel missing-close price P&L against an independent ledger.

--self-check verifies hand-calculated recovery, blocked-entry and frozen-budget
cases. --server builds synthetic wide panels and compares HTTP timeseries with
those cases and complete-price baselines. Each server is isolated and stopped.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from missing_close_oracle import LedgerConfig, LedgerError, run_ledger  # noqa: E402

TOLERANCE = 1e-9
NAN = float("nan")
SERIES = ("cumPnl", "cumPnlGross", "turnover", "maxWeight", "drawdown",
          "nonzeroCount", "frozenGross")
DIAGNOSTICS = {
    "missingHeldBars": "missing_held_count",
    "blockedOrders": "blocked_order_count",
    "blockedTradeWeight": "blocked_trade_weight",
    "missingEntries": "missing_entry_count",
    "resumedHoldings": "resumed_holding_count",
    "frozenGrossSum": "frozen_gross",
    "terminalFrozenGross": "terminal_frozen_gross",
}


@dataclass
class Scenario:
    name: str
    close: np.ndarray
    signal: np.ndarray
    expected: dict[int, dict[str, float]] = field(default_factory=dict)
    mask: np.ndarray | None = None
    sim_start: int = 0
    reject: bool = False

    def ledger(self, policy: str = "carry"):
        config = LedgerConfig(fee_rate=0.0005, policy=policy,
                              sim_start_index=self.sim_start)
        # DataPanel stores both wide fields as float32 before simulation.
        close = self.close.astype(np.float32).astype(np.float64)
        signal = self.signal.astype(np.float32).astype(np.float64)
        return run_ledger(close, signal, self.mask, config=config)


def scenarios() -> list[Scenario]:
    panel = lambda rows: np.array(rows, dtype=np.float64)
    cases = [
        Scenario("hold_gap_recovery",
                 panel([[100, 100], [NAN, 100], [NAN, 100], [80, 100]]),
                 panel([[1, 0], [1, 0], [1, 0], [0, 1]]),
                 {1: {"weight_0": 1, "ret": 0},
                  2: {"weight_0": 1, "ret": 0},
                  3: {"gross_pnl": -0.2, "turnover": 2, "ret": -0.201}}),
        Scenario("blocked_entry",
                 panel([[100, 100], [NAN, 100], [NAN, 100], [80, 100]]),
                 panel([[0, 1], [1, 0], [1, 0], [1, 0]]),
                 {1: {"weight_0": 0}, 2: {"weight_0": 0},
                  3: {"gross_pnl": 0, "weight_0": 1}}),
        Scenario("pending_replacement",
                 panel([[100, 100], [NAN, 100], [NAN, 100], [100, 100]]),
                 panel([[1, 0], [0, 0], [-1, 0], [1, 0]]),
                 {3: {"weight_0": 1, "turnover": 0, "fee": 0}}),
        Scenario("frozen_budget",
                 panel([[100, 100, 100], [NAN, 100, 100],
                        [NAN, 100, 100], [80, 100, 100]]),
                 panel([[6, 2, 2], [3, 1, 0], [0, 1, -1], [0, 1, -1]]),
                 {0: {"weight_0": 0.6, "weight_1": 0.2},
                  1: {"weight_0": 0.6, "weight_1": 0.25, "turnover": 0.25},
                  2: {"weight_0": 0.6, "weight_1": 0.2, "weight_2": -0.2},
                  3: {"gross_pnl": -0.12, "weight_0": 0, "turnover": 1.2}}),
        Scenario("two_frozen_budget",
                 panel([[100, 100, 100, 100], [NAN, NAN, 100, 100],
                        [100, 100, 100, 100]]),
                 panel([[4, 3, 2, 1], [0, 0, 1, 1], [0, 0, 1, 1]]),
                 {1: {"weight_0": 0.4, "weight_1": 0.3,
                      "weight_2": 0.15, "turnover": 0.1}}),
    ]
    rng = np.random.default_rng(20260911)
    close = np.round(np.abs(100 + np.cumsum(rng.normal(0, 1.5, (12, 4)), axis=0)) + 20, 4)
    signal = np.round(rng.normal(0, 1, (12, 4)), 4)
    signal[4, 1] = NAN
    signal[7, :] = 0
    mask = np.ones((12, 4), dtype=bool)
    mask[6:, 3] = False
    for start in (0, 2):
        cases.append(Scenario(f"clean_start{start}", close, signal,
                              mask=mask, sim_start=start))
    for value, label in ((0.0, "zero"), (-1.0, "negative"), (np.inf, "infinite")):
        cases.append(Scenario(f"abnormal_{label}",
                              panel([[100, 100], [value, 100], [100, 100]]),
                              panel([[1, 0]] * 3), reject=True))
    return cases


def self_check(cases: list[Scenario]) -> dict:
    rows = []
    for case in cases:
        if case.reject:
            for policy in ("carry", "error"):
                try:
                    case.ledger(policy)
                except LedgerError:
                    verdict = "PASS"
                else:
                    verdict = "FAIL"
                rows.append({"case": case.name, "check": f"{policy} rejects", "verdict": verdict})
            continue
        result = case.ledger()
        for bar, checks in case.expected.items():
            for key, expected in checks.items():
                if key.startswith("weight_"):
                    actual = result.weights[bar, int(key.split("_")[1])]
                else:
                    actual = getattr(result, key)[bar]
                rows.append({"case": case.name, "check": f"bar {bar} {key}",
                             "expected": expected, "actual": float(actual),
                             "verdict": "PASS" if abs(actual - expected) <= TOLERANCE else "FAIL"})
    return {"rows": rows, "checks": len(rows),
            "failures": sum(row["verdict"] != "PASS" for row in rows)}


def request(base: str, path: str, payload=None, timeout: float = 120.0):
    data = None if payload is None else json.dumps(payload).encode()
    req = Request(base + path, data=data, headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=timeout) as response:
        return json.loads(response.read())


def free_port() -> int:
    for port in range(18120, 18140):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("No free test server port")


@contextmanager
def server(binary: Path, config: Path, log_path: Path):
    port = free_port()
    with log_path.open("wb") as log:
        process = subprocess.Popen([str(binary), str(config)], stdout=log,
                                   stderr=subprocess.STDOUT,
                                   env=dict(os.environ, LONGSHORT_PORT=str(port)))
        base = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + 60
            while True:
                if process.poll() is not None:
                    raise RuntimeError(f"Test server exited: {log_path.read_text()}")
                try:
                    request(base, "/api/health", timeout=5)
                    break
                except (URLError, ConnectionError, TimeoutError):
                    if time.monotonic() >= deadline:
                        raise RuntimeError(f"Test server did not start: {log_path}")
                    time.sleep(0.1)
            yield base
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def write_fixture(directory: Path, case: Scenario) -> tuple[Path, pd.DatetimeIndex]:
    directory.mkdir(parents=True, exist_ok=True)
    dates = pd.date_range("2026-01-05", periods=len(case.close), freq="D")
    symbols = [f"S{i}" for i in range(case.close.shape[1])]
    pd.DataFrame(case.close, index=dates, columns=symbols).to_parquet(directory / "close.parquet")
    pd.DataFrame(case.signal, index=dates, columns=symbols).to_parquet(directory / "signal.parquet")
    membership = np.ones_like(case.close, dtype=bool) if case.mask is None else case.mask
    pd.DataFrame(membership, index=dates, columns=symbols).to_parquet(directory / "universe.parquet")
    config = {"datasets": [{"name": "synthetic", "format": "wide", "path": ".",
                            "fields": {"close": "close.parquet", "raw_signal": "signal.parquet"}}],
              "universe": {"path": "universe.parquet", "symbols": symbols},
              "backtest": {"start_date": str(dates[0].date()), "end_date": str(dates[-1].date()),
                           "initial_capital": 1000000, "transaction_fee": 5,
                           "bars_per_year": 252,
                           "missing_close_policy": "carry"}}
    path = directory / "config.yaml"
    path.write_text(json.dumps(config))
    return path, dates


def compare(case: Scenario, body: dict, policy: str) -> list[str]:
    result = case.ledger(policy)
    expected = result.http_timeseries()
    timeseries = body.get("timeseries") or {}
    problems = []
    for key in SERIES:
        if key not in timeseries:
            problems.append(f"timeseries.{key} missing")
            continue
        actual = np.asarray(timeseries[key], dtype=float)
        wanted = np.asarray(expected[key], dtype=float)
        if len(actual) != len(wanted):
            problems.append(f"timeseries.{key} length {len(actual)} != {len(wanted)}")
        elif len(actual) and np.max(np.abs(actual - wanted)) > TOLERANCE:
            problems.append(f"timeseries.{key} differs by {np.max(np.abs(actual - wanted)):g}")
    totals = result.diagnostic_totals()
    reported = body.get("missingData")
    if not isinstance(reported, dict):
        problems.append("missingData object missing")
    else:
        for key, source in DIAGNOSTICS.items():
            if key not in reported:
                problems.append(f"missingData.{key} missing")
            elif abs(float(reported[key]) - float(totals[source])) > TOLERANCE:
                problems.append(f"missingData.{key} differs")
    return problems


def http_check(binary: Path, cases: list[Scenario], root: Path) -> dict:
    results = []
    for case in cases:
        directory = root / case.name
        config, dates = write_fixture(directory, case)
        try:
            with server(binary, config, directory / "server.log") as base:
                for policy in ("carry", "error"):
                    payload = {"xml": '<data expr="raw_signal"/>',
                               "simStart": str(dates[case.sim_start].date()),
                               "isStart": str(dates[0].date()),
                               "osStart": str(dates[-1].date()),
                               "missingClose": policy}
                    try:
                        case.ledger(policy)
                    except LedgerError:
                        expected_reject = True
                    else:
                        expected_reject = False
                    try:
                        body = request(base, "/api/simulate", payload)
                    except HTTPError as error:
                        problems = [] if expected_reject and error.code == 400 else [f"unexpected HTTP {error.code}"]
                    else:
                        problems = ["expected HTTP 400"] if expected_reject else compare(case, body, policy)
                    results.append({"case": case.name, "policy": policy,
                                    "verdict": "PASS" if not problems else "FAIL",
                                    "problems": problems})
        except Exception as error:
            results.append({"case": case.name, "verdict": "ERROR", "problems": [str(error)]})
    return {"cases": results,
            "failures": sum(row["verdict"] != "PASS" for row in results)}


def self_test() -> dict:
    case = scenarios()[0]
    result = case.ledger()
    expected = result.http_timeseries()
    totals = result.diagnostic_totals()
    good = {"timeseries": {key: list(np.asarray(expected[key])) for key in SERIES},
            "missingData": {key: totals[source] for key, source in DIAGNOSTICS.items()}}
    probes = [
        ("matching response", good, False),
        ("missing series", {**good, "timeseries": {key: value for key, value in good["timeseries"].items()
                                                   if key != "frozenGross"}}, True),
        ("wrong cumulative P&L", {**good, "timeseries": {**good["timeseries"],
                                     "cumPnl": [value + 0.1 for value in good["timeseries"]["cumPnl"]]}}, True),
        ("missing diagnostic", {**good, "missingData": {key: value for key, value in good["missingData"].items()
                                                       if key != "blockedOrders"}}, True),
    ]
    rows = [{"probe": name, "verdict": "PASS" if bool(compare(case, response, "carry")) == bad else "FAIL"}
            for name, response, bad in probes]
    return {"rows": rows, "failures": sum(row["verdict"] != "PASS" for row in rows)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--server", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--keep-fixtures", type=Path)
    args = parser.parse_args()
    if not (args.self_check or args.self_test or args.server):
        parser.error("choose --self-check, --self-test, --server, or a combination")
    report = {"tolerance": TOLERANCE}
    status = 0
    cases = scenarios()
    if args.self_check:
        report["self_check"] = self_check(cases)
        print(f"self-check: {report['self_check']['checks']} checks, {report['self_check']['failures']} failures")
        status |= 1 if report["self_check"]["failures"] else 0
    if args.self_test:
        report["self_test"] = self_test()
        print(f"self-test: {len(report['self_test']['rows'])} probes, {report['self_test']['failures']} failures")
        status |= 4 if report["self_test"]["failures"] else 0
    if args.server:
        binary = args.server.resolve()
        if not binary.exists():
            parser.error(f"server binary not found: {binary}")
        if args.keep_fixtures:
            args.keep_fixtures.mkdir(parents=True, exist_ok=True)
            report["http"] = http_check(binary, cases, args.keep_fixtures)
        else:
            with tempfile.TemporaryDirectory(prefix="longshort_missing_data_") as temporary:
                report["http"] = http_check(binary, cases, Path(temporary))
        print(f"HTTP: {len(report['http']['cases'])} cases, {report['http']['failures']} failures")
        for row in report["http"]["cases"]:
            if row["verdict"] != "PASS":
                print(f"  {row['case']}: {', '.join(row['problems'])}")
        status |= 2 if report["http"]["failures"] else 0
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    return status


if __name__ == "__main__":
    sys.exit(main())
