#!/usr/bin/env python3
"""Check close-to-close universe/start-date PnL against the team's StatsHandler.run.

Requires numpy, pandas and pyarrow. Uses synthetic wide files and an isolated
server process; does not change team code/data or contact the database. Trading
returns are explicitly zero for the platform's close-execution assumption.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd


def actual_stats_run(path):
    source = path.read_text()
    tree = ast.parse(source, filename=str(path))
    handler = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "StatsHandler")
    method = next(node for node in handler.body if isinstance(node, ast.FunctionDef) and node.name == "run")
    # Execute the real method body without importing configuration/DB dependencies.
    unit = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
    namespace = {"np": np, "pd": pd}
    exec(compile(unit, str(path), "exec"), namespace)
    return namespace["run"], hashlib.sha256(source.encode()).hexdigest()


def request(base, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = Request(base+path, data=data, headers={"Content-Type":"application/json"})
    with urlopen(req, timeout=30) as response:
        return json.loads(response.read())


@contextmanager
def server(binary, config, log_path):
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        port = candidate.getsockname()[1]
    environment = dict(os.environ, LONGSHORT_PORT=str(port))
    with log_path.open("wb") as log:
        process = subprocess.Popen([str(binary),str(config)], stdout=log, stderr=subprocess.STDOUT, env=environment)
        base = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic()+20
            while True:
                if process.poll() is not None:
                    raise RuntimeError(f"Test server exited: {log_path.read_text()}")
                try:
                    request(base,"/api/health")
                    break
                except (URLError,ConnectionError,TimeoutError):
                    if time.monotonic() >= deadline:
                        raise RuntimeError(f"Test server timeout: {log_path}")
                    time.sleep(0.05)
            yield base
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def run_case(root, binary, stats_run, name, mask, start=0, missing_exit_close=False, mode=1):
    directory = root/name
    directory.mkdir()
    dates = pd.date_range("2026-01-01",periods=5)
    close = pd.DataFrame({"A":[100.,100.,90.,81.,72.],"B":[100.]*5},index=dates)
    signals = pd.DataFrame({"A":[1.]*5,"B":[-1.]*5},index=dates)
    membership = pd.DataFrame(mask,index=dates,columns=["A","B"],dtype=bool)
    if missing_exit_close:
        close.iloc[3,0] = np.nan
    for filename, frame in (("close",close),("signal",signals),("universe",membership)):
        frame.to_parquet(directory/(filename+".parquet"))
    config = {
        "datasets":[{"name":"synthetic","format":"wide","path":".",
                     "fields":{"close":"close.parquet","raw_signal":"signal.parquet"}}],
        "universe":{"path":"universe.parquet","symbols":["A","B"]},
        "backtest":{"start_date":"2026-01-01","end_date":"2026-01-05", "initial_capital":1000000,
                    "transaction_fee":10,"bars_per_year":252},
    }
    config_path = directory/"config.json"
    config_path.write_text(json.dumps(config))
    payload = {"xml":'<data expr="raw_signal"/>',"simStart":str(dates[start].date()),
               "isStart":"2026-01-01","osStart":"2026-01-05","mode":mode}
    with server(binary,config_path,directory/"server.log") as base:
        if missing_exit_close:
            try:
                request(base,"/api/simulate",payload)
            except HTTPError as error:
                body = json.loads(error.read())
                assert "invalid close" in body.get("error", ""), body
                return {"case":name,"result":"held missing close rejected","status":error.code}
            raise AssertionError("Missing liquidation close was silently accepted")
        response = request(base,"/api/simulate",payload)
        configuration = request(base,"/api/config")
        assert configuration["barsPerYear"] == 252
    # D1 matches pv_champ_peer: mask signal -> shift(1) -> gross normalization.
    # D0 exercises the existing undelayed engine mode against the same PnL oracle.
    delayed = signals.where(membership).shift(mode)
    positions = delayed.div(delayed.abs().sum(axis=1).replace(0,np.nan),axis=0).fillna(0.0)
    positions = positions.iloc[start:]
    hr = close.div(close.shift(1)).sub(1).fillna(0.0).iloc[start:]
    zeros = hr*0.0
    actual = stats_run(SimpleNamespace(pos=positions,hr=hr,tr=zeros,opr=zeros,cost=0.001))
    begin = max(start,mode)
    expected = actual["PnL"].loc[dates[begin:]].to_numpy()
    timeseries = response["timeseries"]
    assert timeseries["timestamps"] == [str(date.date()) for date in dates[begin:]]
    observed = np.diff(np.r_[0.0,np.asarray(timeseries["cumPnl"])])
    np.testing.assert_allclose(observed,expected,rtol=0,atol=1e-12)
    expected_turnover = positions.diff().fillna(positions).abs().sum(axis=1).loc[dates[begin:]].to_numpy()
    np.testing.assert_allclose(np.asarray(timeseries["turnover"])/100,expected_turnover,rtol=0,atol=1e-12)
    expected_counts = (positions.abs()>1e-12).sum(axis=1).loc[dates[begin:]].tolist()
    assert timeseries["nonzeroCount"] == expected_counts
    return {"case":name,"max_abs_pnl_error":float(np.max(np.abs(observed-expected))),
            "daily_pnl":observed.tolist(),"position_counts":expected_counts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server",type=Path,required=True)
    parser.add_argument("--team-sim",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args = parser.parse_args()
    stats_path = args.team_sim/"utils"/"sim_util"/"handlers.py"
    stats_run, digest = actual_stats_run(stats_path)
    binary = args.server.resolve()
    with tempfile.TemporaryDirectory(prefix="longshort_team_parity_") as temporary:
        root = Path(temporary)
        exit_one = [[1,1],[1,1],[0,1],[0,1],[0,1]]
        cases = [
            run_case(root,binary,stats_run,"delayed_exit",exit_one),
            run_case(root,binary,stats_run,"start_without_prior_holdings",exit_one,start=2),
            run_case(root,binary,stats_run,"empty_universe_exit",[[1,1],[1,1],[0,0],[0,0],[0,0]]),
            run_case(root,binary,stats_run,"same_close_D0_exit",exit_one,mode=0),
            run_case(root,binary,stats_run,"same_close_D0_start",exit_one,start=2,mode=0),
            run_case(root,binary,stats_run,"missing_exit_close",exit_one,missing_exit_close=True),
        ]
    report = {"oracle":str(stats_path),"source_sha256":digest,
              "scope":"actual StatsHandler.run PnL; trading returns=0; PV signal-date universe mask then D1 shift(1), plus existing D0 mode",
              "cases":cases}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps(report,indent=2))


if __name__ == "__main__":
    main()
