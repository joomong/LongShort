#!/home/jgo/usr/venv-3.11/bin/python
"""V1 — independent P&L reconciliation of the engine's corporate-action accounting (rc2 BRIEF §3-5).

What it does (every step is re-runnable and exits non-zero on any mismatch beyond tolerance):

  A. Builds small wide-panel fixtures (split_adj basis, float32 closes) from REAL raw closes of the rc1
     cache (base_data.parquet, factors/split.parquet, master/current.parquet) for the 5 GS distribution
     cases (HON/HONA, DD/Q, BDX/WAT, CMCSA/VSNT, HON/SOLS) with a ledger computed here from the §3-3
     unit formulas, and for the 7 GS merger cases through the existing `events:` path (cache-unit
     cash/ratio + staleness anchors per CLAUDE.md). Positions come from a fixed-signal strategy
     (wide field `sig`), so the parent weight on the ex-date is the case's long/short weight.
  B. Runs the engine (D1 and D0) through the production loader (`DataPanel::load` from the YAML config:
     prices, `events:`, `backtest.corporate_actions` incl. the staleness guard) with a small C++ probe
     linked against the build's libengine.a, and compares
       - every bar's gross P&L / turnover / weights with an independent Python re-implementation of the
         documented accounting (fixed book, cash-style ex-date legs, pending/unpriced children, events
         settlement) — tolerance `--tol` (default 1e-9),
       - the ex-date P&L with gs_expected.csv (w × ret_cash_style): strictly (1e-9) against the same
         formula evaluated on the fixture's float32 marks, and informationally against the GS decimal
         marks (the residual is float32 storage, quantified per case),
       - the created child weight with childRatioCache × w × child_close / mark,
       - merger cases: the settlement bar / acquirer price date the engine used vs the GS convention
         (differences are reported, not "fixed").
  C2/C3. Split-path transitions (events settlement + ledger leg on the same parent and bar, E1 §9 rule:
     legs on the settlement bar book against the pre-settlement exposure/mark; a ledger cash leg is deferred
     when the events row carries cash, a ledger stock leg naming the successor is deferred to the transfer),
     synthetic long/short + the real SLM 2014-05-01 case in three keyings.
  C. Synthetic cases (own parquet): long/short dividend, spin long/short, existing child of opposite sign,
     fractional ratio, halted child (pending → first quote), child not in panel (unpriced), same-day
     cash + stock (two rows), reverse split + spin (HON-like), liquidation fee/turnover, child with a
     nonzero strategy target, warmup bar, parent sold on / bought on the ex-date (D1), parent frozen on
     the ex-date, a stale-ledger refusal, and (Y1) the export's marker rows (cash without an amount, stock
     without a child, `markerReason`) on a held parent: P&L bit-identical to the run without them, listed
     as `unconfirmed` (unresolved_leg) with the held weight, refused under `unresolved_policy: strict`,
     and silent on an unheld parent; (Y1-B) a stock leg whose child is outside the panel but carries a usable
     per-share value (`childValuePerShareCache`, P10 (2)(3)(4)) books it as P&L without a position (`ledgerValueCash`).
  G. (Y1-A) `export_events`: the published events file is raw-unit; the loader restates it in split_adj units and
     re-anchors it on the export panel. Synthetic fixture cache + the four published pairs the raw file failed on
     (BRCM->AVGO, TYC->JCI, EIDP->DD, QRTE.A->QVCA.Q): the engine accepts the export (guard checked), refuses the raw
     file where a split intervenes, and settles at the raw-unit return.
  D. Double-counting check: engine-with-ledger ex-date parent P&L == w × returns_total v2 (retFlag 2)
     for the same parent weight, i.e. an engine that used returns_total for holding P&L must not also
     book the ledger.
  E. Real-data export identity: the R1 `engine/ca_ledger_cache.parquet` rows for HON 2026-06-29 and
     DD 2025-11-03 are checked against unit conversions recomputed here from raw prices and split
     factors, against the reference loader's split_adj panel, and the engine is run with the R1 file
     itself as the ledger (must reproduce the own-ledger run bit for bit).
  F. Optional HTTP cross-check (`--http`): `longshort-server` on a private port, `/api/simulate` with
     `<data expr="sig" delay="0"/>`, comparing cumPnlGross differences, dividendPnl/distributionPnl
     series, distributionLog and corporateActions totals with the probe run.

Usage:
  recon_engine_ca.py --build-dir /tmp/longshort_rc2_V1/build --ledger .../R1/engine/ca_ledger_cache.parquet \
      --out /path/to/out [--source-dir cpp-engine] [--cache-root rc1 stage] [--gs gs_expected.csv] \
      [--returns-v2 .../R2/derived/returns_total.parquet] [--http] [--tol 1e-9]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

SCRATCH = Path("/tmp/claude-1008/-home-jgo/3f2abe1d-a2a7-4017-9ac1-2c7cb961e311/scratchpad")
DEFAULTS = {
    "cache_root": SCRATCH / "stage_v21" / "build_20260916_rc1",
    "gs": SCRATCH / "stage_rc2" / "V1" / "gs_expected.csv",
    "ledger": SCRATCH / "stage_rc2" / "R1" / "engine" / "ca_ledger_cache.parquet",
    "returns_v2": SCRATCH / "stage_rc2" / "R2" / "derived" / "returns_total.parquet",
    "out": SCRATCH / "stage_rc2" / "V1" / "out",
    "source_dir": Path("/home/jgo/longshort_platform/cpp-engine"),
}
FEE_BPS = 5.0
FEE = FEE_BPS * 1e-4
NS_PER_DAY = 86_400_000_000_000
# GS child names that are not tickers.
GS_CHILD_TICKER = {"HONEYWELL AEROSPACE": "HONA", "SOLSTICE": "SOLS"}

# ----------------------------------------------------------------------------------------------------
# C++ probe: production loader + Simulator::run_from_signals, full-precision JSON dump.
# ----------------------------------------------------------------------------------------------------
PROBE_SOURCE = r'''
// V1 reconciliation probe: loads a YAML config through the production loader (prices, events,
// corporate-action ledger with its staleness guard), takes the per-bar strategy signal from the
// wide field `sig` and drives Simulator::run_from_signals for one execution mode, dumping every
// accounting row at full precision (%.17g) for the Python reference model.
#include <cmath>
#include <cstdio>
#include <fstream>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>
#include <json/json.h>
#include "data/config_loader.h"
#include "data/data_panel.h"
#include "engine/simulator.h"

namespace {
Json::Value number(double v) {
    if (std::isnan(v)) return Json::Value(Json::nullValue);
    if (std::isinf(v)) return Json::Value(v > 0 ? "inf" : "-inf");
    return Json::Value(v);
}
Json::Value vec(const std::vector<double>& xs) {
    Json::Value out(Json::arrayValue);
    for (double x : xs) out.append(number(x));
    return out;
}
struct Rows {
    std::vector<std::vector<double>> weights, trades, desired, marks_before, marks_after, delisting_returns;
    std::vector<double> gross, fee, net, turnover;
    std::vector<int> trading;
    std::vector<MissingDataDiagnostics> missing;
    std::vector<CorporateActionDiagnostics> actions;
    void record(const AccountingRowView& r) {
        weights.push_back(r.weights); trades.push_back(r.market_trades); desired.push_back(r.desired_weights);
        marks_before.push_back(r.marks_before); marks_after.push_back(r.marks_after);
        delisting_returns.push_back(r.delisting_returns);
        gross.push_back(r.gross_pnl); fee.push_back(r.fee); net.push_back(r.net_pnl); turnover.push_back(r.turnover);
        trading.push_back(r.trading_enabled ? 1 : 0);
        missing.push_back(r.diagnostics); actions.push_back(r.corporate_actions);
    }
};
}  // namespace

int main(int argc, char** argv) {
    if (argc < 4) { std::fprintf(stderr, "usage: recon_probe CONFIG MODE OUT.json\n"); return 2; }
    try {
        const BacktestConfig config = BacktestConfig::from_yaml(argv[1]);
        const int mode = std::stoi(argv[2]);
        DataPanel panel = DataPanel::load(config);
        if (!panel.has_column("sig")) throw std::runtime_error("fixture has no 'sig' field");
        std::vector<std::vector<float>> signals(panel.T, std::vector<float>(panel.N, 0.0f));
        for (unsigned di = 0; di < panel.T; ++di) {
            const float* row = panel.row("sig", di);
            signals[di].assign(row, row + panel.N);
        }
        Rows rows;
        SimulatorConfig sc;
        sc.T = panel.T; sc.N = panel.N; sc.fee_rate = config.transaction_fee_rate; sc.mode = mode;
        sc.sim_start_index = panel.trade_start_index;
        sc.missing_close_policy = config.missing_close_policy == "error" ? MissingClosePolicy::Error : MissingClosePolicy::Carry;
        sc.default_delisting_return = config.default_delisting_return;
        sc.stale_quote_settle_bars = config.stale_quote_settle_bars;
        sc.collect_settlements = true;
        sc.collect_distributions = true;
        sc.apply_dividends = config.corporate_actions.apply_dividends;
        sc.apply_distributions = config.corporate_actions.apply_distributions;
        sc.unpriced_child = config.corporate_actions.unpriced_child == "drop" ? UnpricedChildPolicy::Drop : UnpricedChildPolicy::Pending;
        sc.unresolved_policy = config.corporate_actions.unresolved_policy == "strict" ? UnresolvedPolicy::Strict : UnresolvedPolicy::Warn;
        sc.accounting_row_callback = [&rows](const AccountingRowView& r) { rows.record(r); };
        const SimulatorOutput out = Simulator(panel, sc).run_from_signals(signals);

        Json::Value j;
        j["mode"] = mode;
        j["T"] = panel.T; j["N"] = panel.N; j["tradeStartIndex"] = panel.trade_start_index;
        j["feeRate"] = config.transaction_fee_rate;
        for (const auto& s : panel.symbols) j["symbols"].append(s);
        for (int64_t ts : panel.timestamps) j["timestamps"].append(Json::Int64(ts));
        Json::Value close(Json::arrayValue), sig(Json::arrayValue);
        for (unsigned di = 0; di < panel.T; ++di) {
            const float* c = panel.row("close", di);
            Json::Value crow(Json::arrayValue), srow(Json::arrayValue);
            for (unsigned n = 0; n < panel.N; ++n) {
                crow.append(number(static_cast<double>(c[n])));
                srow.append(number(static_cast<double>(signals[di][n])));
            }
            close.append(crow); sig.append(srow);
        }
        j["close"] = close; j["sig"] = sig;
        Json::Value legs(Json::arrayValue);
        for (const auto& leg : panel.corporate_actions) {
            Json::Value l;
            l["bar"] = leg.bar; l["parent"] = leg.parent; l["child"] = leg.child; l["stockLeg"] = leg.stock_leg;
            l["rights"] = leg.rights; l["estimate"] = leg.estimate; l["ledgerValueUsable"] = leg.ledger_value_usable;
            l["unresolved"] = leg.unresolved; l["cash"] = number(leg.cash); l["childRatio"] = number(leg.child_ratio);
            l["childClose"] = number(leg.child_close); l["eventType"] = leg.event_type; l["valueSource"] = leg.value_source;
            legs.append(l);
        }
        j["legs"] = legs;
        Json::Value report;
        report["loaded"] = panel.corporate_actions_loaded;
        report["rows"] = Json::UInt64(panel.corporate_action_rows);
        report["rowsMatched"] = Json::UInt64(panel.corporate_action_rows_matched);
        report["rowsQuantity"] = Json::UInt64(panel.corporate_action_rows_quantity);
        report["rowsUnresolved"] = Json::UInt64(panel.corporate_action_rows_unresolved);
        report["legs"] = Json::UInt64(panel.corporate_action_legs);
        report["legsEstimated"] = Json::UInt64(panel.corporate_action_legs_estimated);
        report["legsUnbookable"] = Json::UInt64(panel.corporate_action_legs_unbookable);
        report["legsValueOnly"] = Json::UInt64(panel.corporate_action_legs_value_only);
        report["legsRights"] = Json::UInt64(panel.corporate_action_legs_rights);
        report["legsAfterParentEnd"] = Json::UInt64(panel.corporate_action_legs_after_parent_end);
        report["legsOutOfRange"] = Json::UInt64(panel.corporate_action_out_of_range);
        report["unpricedChildren"] = Json::UInt64(panel.corporate_action_unpriced_children);
        report["priceChecks"] = Json::UInt64(panel.corporate_action_price_checks);
        report["priceChecksNotEvaluable"] = Json::UInt64(panel.corporate_action_price_checks_not_evaluable);
        j["ledgerReport"] = report;
        Json::Value events(Json::arrayValue);
        for (unsigned n = 0; n < panel.N && !panel.settlement_bar.empty(); ++n) {
            Json::Value e;
            e["settlementBar"] = panel.settlement_bar[n];
            e["successor"] = panel.settlement_successor.empty() ? -1 : panel.settlement_successor[n];
            e["ratio"] = panel.settlement_successor_ratio.empty() ? Json::Value(Json::nullValue) : number(panel.settlement_successor_ratio[n]);
            e["cash"] = panel.settlement_cash.empty() ? Json::Value(Json::nullValue) : number(panel.settlement_cash[n]);
            e["hasStockLeg"] = panel.settlement_has_stock_leg.empty() ? false : panel.settlement_has_stock_leg[n] != 0;
            e["hasCashLeg"] = panel.settlement_has_cash_leg.empty() ? false : panel.settlement_has_cash_leg[n] != 0;
            events.append(e);
        }
        j["events"] = events;
        j["eventsPriceChecks"] = Json::UInt64(panel.events_price_checks);
        j["eventsRowsMatched"] = Json::UInt64(panel.events_rows_matched);
        Json::Value bars(Json::arrayValue);
        for (size_t t = 0; t < rows.gross.size(); ++t) {
            Json::Value b;
            b["trading"] = rows.trading[t];
            b["gross"] = number(rows.gross[t]); b["fee"] = number(rows.fee[t]); b["net"] = number(rows.net[t]);
            b["turnover"] = number(rows.turnover[t]);
            b["ret"] = number(out.ret[t]); b["outTurnover"] = number(out.turnover[t]);
            b["longWeight"] = number(out.long_weight[t]); b["shortWeight"] = number(out.short_weight[t]);
            b["maxWeight"] = number(out.max_weight[t]); b["positionCount"] = out.position_count[t];
            b["weights"] = vec(rows.weights[t]); b["trades"] = vec(rows.trades[t]); b["desired"] = vec(rows.desired[t]);
            b["marksBefore"] = vec(rows.marks_before[t]); b["marksAfter"] = vec(rows.marks_after[t]);
            b["delistingReturns"] = vec(rows.delisting_returns[t]);
            const auto& a = rows.actions[t];
            Json::Value ca;
            ca["dividendCount"] = a.dividend_count; ca["dividendPnl"] = number(a.dividend_pnl);
            ca["distributionCount"] = a.distribution_count; ca["distributionPnl"] = number(a.distribution_pnl);
            ca["childPositionsCreated"] = a.child_positions_created; ca["distributionEstimated"] = a.distribution_estimated;
            ca["distributionPending"] = a.distribution_pending; ca["distributionUnpriced"] = a.distribution_unpriced;
            ca["distributionValueOnly"] = a.distribution_value_only;
            ca["legsUnresolved"] = a.legs_unresolved;
            ca["legsOnSettlementBar"] = a.legs_on_settlement_bar; ca["legsDeferredToEvents"] = a.legs_deferred_to_events;
            ca["rightsExpired"] = a.rights_expired;
            ca["dividendEstimated"] = a.dividend_estimated;
            ca["unconfirmedCount"] = a.unconfirmed_count; ca["unconfirmedAbsWeight"] = number(a.unconfirmed_abs_weight);
            b["corporateActions"] = ca;
            const auto& m = rows.missing[t];
            Json::Value md;
            md["missingHeld"] = m.missing_held_count; md["frozenGross"] = number(m.frozen_gross);
            md["blockedOrders"] = m.blocked_order_count; md["delistings"] = m.delisting_count;
            md["delistingPnl"] = number(m.delisting_pnl); md["settledGross"] = number(m.settled_gross);
            md["successorConversions"] = m.successor_conversions; md["convertedGross"] = number(m.converted_gross);
            md["computedSettlements"] = m.computed_settlement_count; md["retirements"] = m.retirement_count;
            md["defaultDelistings"] = m.default_delisting_count;
            b["missingData"] = md;
            bars.append(b);
        }
        j["bars"] = bars;
        Json::Value dist(Json::arrayValue);
        for (const auto& r : out.distributions) {
            Json::Value d;
            d["bar"] = r.bar; d["exBar"] = r.ex_bar; d["parent"] = r.parent; d["child"] = r.child;
            d["parentWeight"] = number(r.parent_weight); d["childWeight"] = number(r.child_weight); d["cashShare"] = number(r.cash_share);
            d["source"] = r.source == DistributionSource::ChildClose ? "childClose" : r.source == DistributionSource::LedgerValue ? "ledgerValue"
                        : r.source == DistributionSource::FirstQuote ? "firstQuote" : r.source == DistributionSource::Expired ? "expired"
                        : r.source == DistributionSource::LedgerValueCash ? "ledgerValueCash" : "unpriced";
            d["estimate"] = r.estimate; d["onSettlementBar"] = r.on_settlement_bar; d["valueSource"] = r.value_source; d["eventType"] = r.event_type;
            dist.append(d);
        }
        j["distributions"] = dist;
        Json::Value setl(Json::arrayValue);
        for (const auto& r : out.settlements) {
            Json::Value s;
            s["bar"] = r.bar; s["asset"] = r.asset; s["exposure"] = number(r.exposure); s["return"] = number(r.settlement_return);
            s["source"] = r.source == SettlementSource::Computed ? "computed" : r.source == SettlementSource::Explicit ? "explicit" : "default";
            s["cashShare"] = number(r.cash_share); s["stockShare"] = number(r.stock_share); s["successor"] = r.successor; s["transferred"] = number(r.transferred);
            setl.append(s);
        }
        j["settlements"] = setl;
        Json::Value unc(Json::arrayValue);
        for (const auto& r : out.unconfirmed) {
            Json::Value u;
            u["bar"] = r.bar; u["asset"] = r.asset; u["weightBefore"] = number(r.weight_before);
            u["kind"] = r.kind == UnconfirmedKind::UnresolvedLeg ? "unresolved_leg" : r.kind == UnconfirmedKind::DefaultSettlement ? "default_settlement"
                      : r.kind == UnconfirmedKind::EstimateValue ? "estimate_value" : "pending_dropped";
            u["detail"] = r.detail;
            unc.append(u);
        }
        j["unconfirmed"] = unc;
        Json::StreamWriterBuilder builder;
        builder["precision"] = 17;
        builder["indentation"] = "";
        std::ofstream fout(argv[3]);
        fout << Json::writeString(builder, j) << "\n";
        return 0;
    } catch (const std::exception& e) {
        std::fprintf(stderr, "ERROR: %s\n", e.what());
        Json::Value j; j["error"] = e.what();
        std::ofstream fout(argv[3]); fout << j.toStyledString();
        return 1;
    }
}
'''


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Probe:
    """Compile the probe against <build>/libengine.a with the build's own compile/link lines."""

    def __init__(self, build_dir: Path, work: Path, rebuild: bool):
        self.build = build_dir.resolve()
        self.work = work
        work.mkdir(parents=True, exist_ok=True)
        cache = self.build / "CMakeCache.txt"
        if not cache.exists():
            raise SystemExit(f"--build-dir {self.build} has no CMakeCache.txt (configure it first)")
        self.source = None
        for line in cache.read_text().splitlines():
            if line.startswith("CMAKE_HOME_DIRECTORY:INTERNAL="):
                self.source = Path(line.split("=", 1)[1])
        if rebuild:
            run(["cmake", "--build", str(self.build), "--target", "engine", "longshort-server", "--parallel", "6"], "cmake --build")
        self.lib = self.build / "libengine.a"
        if not self.lib.exists():
            raise SystemExit(f"{self.lib} missing: build the engine target first (or pass --rebuild)")
        # A probe compiled with headers newer than the library is an ABI gamble: refuse.
        newest = max(p.stat().st_mtime for p in (self.source / "src").rglob("*.h"))
        if newest > self.lib.stat().st_mtime + 1:
            raise SystemExit(f"headers under {self.source}/src are newer than {self.lib}; pass --rebuild")
        cc = json.loads((self.build / "compile_commands.json").read_text())
        entry = next(e for e in cc if e["file"].endswith("tools/data_load_probe.cpp"))
        flags = shlex.split(entry["command"])
        stripped = []
        i = 0
        while i < len(flags):
            if flags[i] in ("-o", "-c"):
                i += 2
                continue
            stripped.append(flags[i])
            i += 1
        src = work / "recon_probe.cpp"
        src.write_text(PROBE_SOURCE)
        obj = work / "recon_probe.o"
        exe = work / "recon_probe"
        run(stripped + ["-o", str(obj), "-c", str(src)], "compile probe")
        link = (self.build / "CMakeFiles" / "data_load_probe.dir" / "link.txt").read_text().split()
        linked = []
        i = 0
        while i < len(link):
            if link[i].endswith("data_load_probe.cpp.o"):
                linked.append(str(obj))
            elif link[i] == "-o":
                linked += ["-o", str(exe)]
                i += 1
            else:
                linked.append(link[i])
            i += 1
        run(linked, "link probe", cwd=self.build)
        self.exe = exe
        self.server = self.build / "longshort-server"

    def run(self, config: Path, mode: int, out_json: Path) -> dict:
        r = subprocess.run([str(self.exe), str(config), str(mode), str(out_json)], capture_output=True, text=True)
        payload = json.loads(out_json.read_text()) if out_json.exists() else {}
        if r.returncode != 0:
            return {"error": payload.get("error", r.stderr.strip()), "stderr": r.stderr.strip(), "rc": r.returncode}
        payload["stderr"] = r.stderr.strip()
        return payload


def run(cmd, what, cwd=None):
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, cwd=cwd)
    if r.returncode != 0:
        raise SystemExit(f"{what} failed (rc {r.returncode}):\n{r.stdout[-2000:]}\n{r.stderr[-4000:]}")
    return r


# ----------------------------------------------------------------------------------------------------
# Real data access (rc1 cache: raw long prices, split-factor intervals, master).
# ----------------------------------------------------------------------------------------------------
class Cache:
    def __init__(self, root: Path):
        self.root = root
        self.master = pq.read_table(root / "master" / "current.parquet").to_pandas()
        self.split = pq.read_table(root / "factors" / "split.parquet").to_pandas()
        cal = pq.read_table(root / "calendar.parquet").to_pandas()
        self.calendar = pd.DatetimeIndex(cal.loc[cal.isTradingDay, "date"].sort_values().values)
        self._raw = {}

    def resolve(self, symbol: str, on: pd.Timestamp) -> int:
        """symbol → tradingItemId whose security window covers `on` (merger targets end on/after on − 10d)."""
        m = self.master
        rows = m[(m.symbol == symbol)]
        rows = rows[(rows.securityStartDate <= on) & (rows.securityEndDate.isna() | (rows.securityEndDate >= on - pd.Timedelta(days=10)))]
        if len(rows) != 1:
            raise SystemExit(f"master/current.parquet: {len(rows)} candidates for {symbol} on {on.date()}: {rows.tradingItemId.tolist()}")
        return int(rows.tradingItemId.iloc[0])

    def raw_closes(self, ids: list[int], start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        """Wide float32 raw closes (calendar rows × ids) from base_data.parquet."""
        t = pq.read_table(self.root / "base_data.parquet", columns=["date", "tradingItemId", "close", "qualityFlag"],
                          filters=[("tradingItemId", "in", ids), ("date", ">=", start), ("date", "<=", end)])
        df = t.to_pandas()
        df = df[df.qualityFlag != 4]          # rows after lifecycle.lastTradeDate are cut from the engine panel
        wide = df.pivot(index="date", columns="tradingItemId", values="close")
        days = self.calendar[(self.calendar >= start) & (self.calendar <= end)]
        wide = wide.reindex(index=days, columns=ids)
        return wide.astype(np.float32)

    def sf(self, tid: int, on: pd.Timestamp) -> float:
        """Split factor interval lookup: validFrom <= on < validTo (NaT = open)."""
        s = self.split[self.split.tradingItemId == tid]
        hit = s[(s.validFrom <= on) & (s.validTo.isna() | (s.validTo > on))]
        if len(hit) != 1:
            raise SystemExit(f"factors/split.parquet: {len(hit)} intervals for {tid} on {on.date()}")
        return float(hit.factor.iloc[0])

    def adj(self, raw: pd.DataFrame) -> pd.DataFrame:
        """split_adj = float32(float64(raw) / sf(date)) — the loader's own definition (SPEC_loader §4)."""
        out = raw.astype(np.float64).copy()
        for tid in raw.columns:
            for d in raw.index:
                if np.isfinite(out.at[d, tid]):
                    out.at[d, tid] = out.at[d, tid] / self.sf(tid, d)
        return out.astype(np.float32)


# ----------------------------------------------------------------------------------------------------
# Fixture: wide parquet panels + ledger + events + YAML.
# ----------------------------------------------------------------------------------------------------
LEDGER_TYPES = {
    "tradingItemId": pa.string(), "exDate": pa.timestamp("ns"), "eventType": pa.string(), "legType": pa.string(),
    "cashPerShareCache": pa.float64(), "childTradingItemId": pa.string(), "childRatioCache": pa.float64(),
    "childCloseAtExCache": pa.float64(), "parentCloseAtExCache": pa.float64(), "parentCloseBeforeCache": pa.float64(),
    "parentFactorCache": pa.float64(), "payDate": pa.timestamp("ns"), "isEstimate": pa.bool_(), "status": pa.string(),
    "childValueSource": pa.string(), "eventId": pa.string(), "markerReason": pa.string(), "childValuePerShareCache": pa.float64(),
    "estimateReason": pa.string(),
}
EVENT_TYPES = {
    "tradingItemId": pa.string(), "securityEndDate": pa.timestamp("ns"), "cashPerShareCache": pa.float64(),
    "successorTradingItemId": pa.string(), "successorRatioCache": pa.float64(), "hasStockLeg": pa.bool_(),
    "hasCashLeg": pa.bool_(), "successorCacheClose": pa.float64(), "successorCacheCloseDate": pa.timestamp("ns"),
    "targetCacheMark": pa.float64(), "targetCacheMarkDate": pa.timestamp("ns"), "settlementReturn": pa.float64(),
}


def ts_ns(x):
    return None if x is None else np.datetime64(pd.Timestamp(x), "ns")


def write_rows(path: Path, rows: list[dict], types: dict):
    cols = {}
    for name, typ in types.items():
        values = [r.get(name) for r in rows]
        if pa.types.is_timestamp(typ):
            values = [ts_ns(v) for v in values]
        elif pa.types.is_floating(typ):
            values = [None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v) for v in values]
        cols[name] = pa.array(values, type=typ)
    pq.write_table(pa.table(cols), path)


@dataclass
class Fixture:
    name: str
    dir: Path
    symbols: list[str]              # lexically sorted, the engine's order
    dates: pd.DatetimeIndex
    close: np.ndarray               # T×N float32
    sig: np.ndarray                 # T×N float32 (NaN = no target)
    ledger_rows: list[dict] = field(default_factory=list)
    event_rows: list[dict] = field(default_factory=list)
    warmup: int = 0
    labels: dict = field(default_factory=dict)   # symbol → ticker
    stale_check: bool = True
    ledger_path_override: Path | None = None
    unresolved_policy: str | None = None     # None = engine default (warn)

    def idx(self, symbol: str) -> int:
        return self.symbols.index(symbol)

    def bar(self, date) -> int:
        d = pd.Timestamp(date)
        hit = np.flatnonzero(self.dates == d)
        if len(hit) != 1:
            raise SystemExit(f"{self.name}: {d.date()} is not a fixture bar")
        return int(hit[0])

    def write(self) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        index = pa.array(self.dates.values.astype("datetime64[ns]"), type=pa.timestamp("ns"))
        for field_name, matrix in (("close", self.close), ("sig", self.sig)):
            arrays = {"__index_level_0__": index}
            for n, s in enumerate(self.symbols):
                arrays[s] = pa.array(np.asarray(matrix[:, n], dtype=np.float32), type=pa.float32())
            pq.write_table(pa.table(arrays), self.dir / f"{field_name}.parquet")
        ledger = self.ledger_path_override
        if ledger is None and self.ledger_rows:
            ledger = self.dir / "ledger.parquet"
            write_rows(ledger, self.ledger_rows, LEDGER_TYPES)
        events = None
        if self.event_rows:
            events = self.dir / "events.parquet"
            write_rows(events, self.event_rows, EVENT_TYPES)
        start = self.dates[self.warmup].strftime("%Y-%m-%d")
        end = self.dates[-1].strftime("%Y-%m-%d")
        lines = [
            "datasets:",
            "  - name: prices",
            "    path: .",
            "    format: wide",
            "    time_col: __index_level_0__",
            "    fields: {close: close.parquet, sig: sig.parquet}",
            "backtest:",
            f"  start_date: '{start}'",
            f"  end_date: '{end}'",
            f"  transaction_fee: {FEE_BPS}",
            f"  warmup_bars: {self.warmup}",
            "  missing_close_policy: carry",
        ]
        if ledger is not None:
            policy = f", unresolved_policy: {self.unresolved_policy}" if self.unresolved_policy else ""
            lines.append(f"  corporate_actions: {{path: '{ledger}', stale_check: {'true' if self.stale_check else 'false'}{policy}}}")
        if events is not None:
            lines += [
                "events:",
                f"  path: '{events}'",
                "  id_col: tradingItemId",
                "  end_date_col: securityEndDate",
                "  cash_col: cashPerShareCache",
                "  successor_col: successorTradingItemId",
                "  successor_ratio_col: successorRatioCache",
                "  stock_leg_col: hasStockLeg",
                "  cash_leg_col: hasCashLeg",
                "  successor_price_col: successorCacheClose",
                "  successor_price_date_col: successorCacheCloseDate",
                "  target_price_col: targetCacheMark",
                "  target_price_date_col: targetCacheMarkDate",
                "  return_col: settlementReturn",
            ]
        cfg = self.dir / "config.yaml"
        cfg.write_text("\n".join(lines) + "\n")
        return cfg


# ----------------------------------------------------------------------------------------------------
# Independent reference model of the documented accounting (BRIEF §3-4, CLAUDE.md missing-close/events,
# docs/corporate_actions_engine_2026-09-17.md §3). Written from the contract, not from simulator.cpp.
# ----------------------------------------------------------------------------------------------------
@dataclass
class RefLeg:
    bar: int
    parent: int
    stock: bool
    cash: float = math.nan
    child: int = -1
    ratio: float = math.nan
    child_close: float = math.nan
    usable: bool = False            # childValueSource in {wi_close, event_value, vendor_implied}
    value: float = math.nan         # childValuePerShareCache: value per parent share, booked as P&L when the child is not in the panel


@dataclass
class RefEvent:
    bar: int                        # settlement opens on this bar (first bar after the end date); -1 none
    cash: float = math.nan
    ratio: float = math.nan
    successor: int = -1
    stock_leg: bool = False         # the events row declares a stock leg (hasStockLeg)
    cash_leg: bool = False          # the events row declares a cash leg (hasCashLeg)
    explicit: float = math.nan


def reference_run(close: np.ndarray, sig: np.ndarray, fee: float, mode: int, sim_start: int,
                  legs: list[RefLeg], events: dict[int, RefEvent], default_delisting: float = 0.0) -> dict:
    T, N = close.shape
    close = close.astype(np.float64)
    sig = sig.astype(np.float64)
    prev_w = np.zeros(N)
    prev_targets = np.zeros(N)
    marks = np.full(N, np.nan)
    retired = np.zeros(N, dtype=bool)
    pending = []
    legs = sorted(legs, key=lambda l: (l.bar, l.parent))
    rows = []
    records = []
    for t in range(T):
        trading = t >= sim_start
        c = close[t]
        quoted = np.isfinite(c) & (c > 0)
        carried = np.where(retired, 0.0, prev_w)
        pnl = 0.0
        turnover = 0.0
        diag = dict(dividendPnl=0.0, distributionPnl=0.0, dividendCount=0, distributionCount=0,
                    childPositionsCreated=0, distributionPending=0, distributionUnpriced=0, delistingPnl=0.0,
                    legsOnSettlementBar=0, legsDeferredToEvents=0)
        # 1. events settlement: retire first, then price every settlement of the bar
        settling = [i for i in range(N) if not retired[i] and i in events and events[i].bar >= 0
                    and t >= events[i].bar and not quoted[i]]
        # Ledger legs dated on the settlement bar are measured against the exposure and mark the
        # settlement itself used (E1 §9 decision), so both are kept before the settlement scales them.
        pre_w = {i: carried[i] for i in settling}
        pre_mark = {i: marks[i] for i in settling}
        for i in settling:
            retired[i] = True
        transfers = np.zeros(N)
        for i in settling:
            ev = events[i]
            succ = ev.successor
            quoted_succ = succ >= 0 and succ != i and not retired[succ] and quoted[succ]
            priced_mark = np.isfinite(marks[i]) and marks[i] > 0
            transferable = quoted_succ and np.isfinite(ev.ratio) and priced_mark
            stock_value = ev.ratio * c[succ] if transferable else 0.0
            cash_value = ev.cash if (ev.cash_leg and np.isfinite(ev.cash)) else 0.0
            proceeds = stock_value + cash_value
            stock_share = stock_value / marks[i] if priced_mark else 0.0
            cash_share = cash_value / marks[i] if priced_mark else 0.0
            computable = priced_mark and (ev.stock_leg or ev.cash_leg) and (not ev.stock_leg or transferable) \
                and (not ev.cash_leg or np.isfinite(ev.cash))
            if computable:
                R = stock_share + cash_share - 1.0
            elif np.isfinite(ev.explicit):
                R = ev.explicit
            else:
                R = default_delisting
            if trading and carried[i] != 0.0:
                pnl += carried[i] * R
                diag["delistingPnl"] += carried[i] * R
                if computable and ev.stock_leg and transferable and proceeds > 0:
                    transfers[succ] += carried[i] * stock_share
            if np.isfinite(marks[i]):
                marks[i] *= 1.0 + R
            carried[i] = 0.0
        # 2. targets from the bar's signal (retired names masked; NaN = 0; carry admits missing quotes)
        s = sig[t].copy()
        s[retired] = np.nan
        finite = np.isfinite(s)
        total = float(np.sum(np.abs(s[finite])))
        targets = np.zeros(N)
        if total > 1e-12:
            targets[finite] = s[finite] / total
        intent = targets if mode == 0 else prev_targets
        # 3. corporate actions on the ex-date bar (held parents only; warmup books nothing)
        received = np.zeros(N)
        if trading:
            cash_share_of = {}
            today = [l for l in legs if l.bar == t]

            def exposure_mark(parent):
                if parent in pre_w:                      # the parent settled on this bar
                    return pre_w[parent], pre_mark[parent], True
                return carried[parent], marks[parent], False

            for l in today:
                if l.stock:
                    continue
                w, m, settled_here = exposure_mark(l.parent)
                if w == 0.0 or not (np.isfinite(m) and m > 0):
                    continue
                if settled_here:
                    diag["legsOnSettlementBar"] += 1
                    if events[l.parent].cash_leg:        # the events row realizes its own cash: count-only
                        diag["legsDeferredToEvents"] += 1
                        continue
                if not np.isfinite(l.cash):
                    continue
                share = l.cash / m
                pnl += w * share
                diag["dividendPnl"] += w * share
                diag["dividendCount"] += 1
                cash_share_of[l.parent] = cash_share_of.get(l.parent, 0.0) + share
            still = []
            for p in pending:
                if retired[p["child"]]:
                    diag["distributionUnpriced"] += 1
                    records.append(dict(bar=t, exBar=p["ex"], parent=p["parent"], child=p["child"], parentWeight=p["w"],
                                        childWeight=0.0, source="unpriced"))
                elif quoted[p["child"]]:
                    cw = p["w"] * p["ratio"] * c[p["child"]] / p["m"]
                    pnl += cw
                    diag["distributionPnl"] += cw
                    diag["childPositionsCreated"] += 1
                    received[p["child"]] += cw
                    records.append(dict(bar=t, exBar=p["ex"], parent=p["parent"], child=p["child"], parentWeight=p["w"],
                                        childWeight=cw, source="firstQuote"))
                else:
                    still.append(p)
            pending = still
            for l in today:
                if not l.stock:
                    continue
                w, m, settled_here = exposure_mark(l.parent)
                if w == 0.0 or not (np.isfinite(m) and m > 0):
                    continue
                if settled_here:
                    diag["legsOnSettlementBar"] += 1
                    if l.child >= 0 and events[l.parent].successor == l.child:   # delivered by the transfer: count-only
                        diag["legsDeferredToEvents"] += 1
                        continue
                diag["distributionCount"] += 1
                if l.child < 0 and l.usable and np.isfinite(l.value) and l.value > 0:
                    # Y1-B: the ledger's value per parent share is P&L like cash; no position exists to hold
                    cw = w * l.value / m
                    pnl += cw
                    diag["distributionPnl"] += cw
                    records.append(dict(bar=t, exBar=t, parent=l.parent, child=-1, parentWeight=w, childWeight=cw,
                                        source="ledgerValueCash", cashShare=cash_share_of.get(l.parent, 0.0), onSettlementBar=settled_here))
                    continue
                if l.child < 0 or not np.isfinite(l.ratio) or retired[l.child]:
                    diag["distributionUnpriced"] += 1
                    records.append(dict(bar=t, exBar=t, parent=l.parent, child=l.child, parentWeight=w, childWeight=0.0,
                                        source="unpriced"))
                    continue
                if quoted[l.child]:
                    cw = w * l.ratio * c[l.child] / m
                    source = "childClose"
                elif l.usable and not np.isfinite(marks[l.child]) and np.isfinite(l.child_close) and l.child_close > 0:
                    marks[l.child] = l.child_close
                    cw = w * l.ratio * l.child_close / m
                    source = "ledgerValue"
                else:
                    pending.append(dict(ex=t, parent=l.parent, child=l.child, w=w, m=m, ratio=l.ratio))
                    continue
                pnl += cw
                diag["distributionPnl"] += cw
                diag["childPositionsCreated"] += 1
                received[l.child] += cw
                records.append(dict(bar=t, exBar=t, parent=l.parent, child=l.child, parentWeight=w, childWeight=cw,
                                    source=source, cashShare=cash_share_of.get(l.parent, 0.0), onSettlementBar=settled_here))
            diag["distributionPending"] = len(pending)
        # 4. holding valuation from the last observed mark; missing quote = frozen
        frozen = 0.0
        for i in range(N):
            if retired[i]:
                continue
            if carried[i] != 0.0 and not np.isfinite(marks[i]):
                raise RuntimeError(f"reference: held {i} without a mark at bar {t}")
            if quoted[i]:
                if trading and carried[i] != 0.0:
                    pnl += carried[i] * (c[i] / marks[i] - 1.0)
                marks[i] = c[i]
            elif trading and carried[i] != 0.0:
                frozen += abs(carried[i])
        carried = carried + transfers
        # 5. execution: quoted names go to target (scaled to the budget left by frozen names)
        if trading:
            scale = 1.0
            if frozen > 0:
                tradable = sum(abs(intent[i]) for i in range(N) if not retired[i] and quoted[i])
                budget = max(0.0, 1.0 - frozen)
                if tradable > budget:
                    scale = budget / tradable
            w = np.zeros(N)
            for i in range(N):
                if retired[i]:
                    w[i] = 0.0
                elif quoted[i]:
                    w[i] = intent[i] * scale
                else:
                    w[i] = carried[i]
            trades = w - carried
            turnover = float(np.sum(np.abs(trades)))
            w = w + received
            ret = pnl - turnover * fee
        else:
            w = np.zeros(N)
            ret = 0.0
            pnl = 0.0
            turnover = 0.0
        rows.append(dict(trading=trading, gross=pnl, turnover=turnover, ret=ret, weights=w.copy(), marks=marks.copy(), **diag))
        prev_w = w
        if mode == 1:
            prev_targets = targets
    return dict(rows=rows, records=records)


# ----------------------------------------------------------------------------------------------------
# Result bookkeeping.
# ----------------------------------------------------------------------------------------------------
class Results:
    def __init__(self, tol: float):
        self.tol = tol
        self.checks = []        # dict(section, case, name, expected, actual, diff, tol, ok, note)
        self.notes = []
        self.tables = {}

    def check(self, section, case, name, expected, actual, tol=None, note="", kind="abs"):
        tol = self.tol if tol is None else tol
        if isinstance(expected, str) or isinstance(actual, str) or isinstance(expected, bool):
            ok = expected == actual
            diff = 0.0 if ok else float("nan")
        else:
            e, a = float(expected), float(actual)
            if math.isnan(e) and math.isnan(a):
                diff, ok = 0.0, True
            else:
                diff = a - e
                scale = max(abs(e), abs(a), 1.0) if kind == "rel" else 1.0
                ok = math.isfinite(diff) and abs(diff) <= tol * scale
        self.checks.append(dict(section=section, case=case, name=name, expected=expected, actual=actual,
                                diff=diff, tol=tol, ok=bool(ok), note=note))
        return ok

    def info(self, section, case, name, value, note=""):
        self.checks.append(dict(section=section, case=case, name=name, expected="", actual=value, diff="",
                                tol="", ok=True, note=note, info=True))

    @property
    def failures(self):
        return [c for c in self.checks if not c["ok"]]


def compare_run(res: Results, section: str, case: str, engine: dict, ref: dict, fx: Fixture, mode: int, tol: float):
    """All bars: gross P&L, turnover, weights, diagnostics engine vs reference."""
    if "error" in engine:
        res.check(section, case, f"D{mode} engine run", "ok", f"error: {engine['error']}")
        return
    bars = engine["bars"]
    rows = ref["rows"]
    res.check(section, case, f"D{mode} symbol order", ",".join(fx.symbols), ",".join(engine["symbols"]))
    # the loader must hand the simulator exactly the float32 closes the fixture stores
    loaded = np.array([[np.nan if v is None else v for v in row] for row in engine["close"]], dtype=np.float32)
    res.check(section, case, f"D{mode} loaded closes bitwise", 0, int(np.sum(~((loaded == fx.close) | (np.isnan(loaded) & np.isnan(fx.close))))))
    res.check(section, case, f"D{mode} bar count", len(rows), len(bars))
    worst = dict(gross=0.0, turnover=0.0, weights=0.0, divPnl=0.0, distPnl=0.0)
    for t, (b, r) in enumerate(zip(bars, rows)):
        worst["gross"] = max(worst["gross"], abs(float(b["gross"]) - r["gross"]))
        worst["turnover"] = max(worst["turnover"], abs(float(b["turnover"]) - r["turnover"]))
        w_eng = np.array([0.0 if v is None else v for v in b["weights"]])
        worst["weights"] = max(worst["weights"], float(np.max(np.abs(w_eng - r["weights"]))) if len(w_eng) else 0.0)
        worst["divPnl"] = max(worst["divPnl"], abs(float(b["corporateActions"]["dividendPnl"]) - r["dividendPnl"]))
        worst["distPnl"] = max(worst["distPnl"], abs(float(b["corporateActions"]["distributionPnl"]) - r["distributionPnl"]))
        for key in ("dividendCount", "distributionCount", "childPositionsCreated", "distributionPending", "distributionUnpriced",
                    "legsOnSettlementBar", "legsDeferredToEvents"):
            if int(b["corporateActions"][key]) != int(r[key]):
                res.check(section, case, f"D{mode} bar {t} {key}", int(r[key]), int(b["corporateActions"][key]))
        # ret == gross - turnover * fee (the engine's own identity)
        if abs(float(b["ret"]) - (float(b["gross"]) - float(b["turnover"]) * engine["feeRate"])) > 1e-15:
            res.check(section, case, f"D{mode} bar {t} ret identity", float(b["gross"]) - float(b["turnover"]) * engine["feeRate"], float(b["ret"]), tol=1e-15)
    for key, value in worst.items():
        res.check(section, case, f"D{mode} max |engine − reference| {key} (all bars)", 0.0, value, tol=tol)
    # distribution records: same count, same child weights
    eng_rec = [d for d in engine["distributions"]]
    ref_rec = ref["records"]
    res.check(section, case, f"D{mode} distribution records", len(ref_rec), len(eng_rec))
    for k, (e, r) in enumerate(zip(eng_rec, ref_rec)):
        res.check(section, case, f"D{mode} record {k} child weight", r["childWeight"], float(e["childWeight"]), tol=tol)
        res.check(section, case, f"D{mode} record {k} source/bar", f"{r['source']}@{r['bar']}", f"{e['source']}@{e['bar']}")
        if "onSettlementBar" in r and "onSettlementBar" in e:
            res.check(section, case, f"D{mode} record {k} onSettlementBar", bool(r["onSettlementBar"]), bool(e["onSettlementBar"]))


# ----------------------------------------------------------------------------------------------------
# A/B. Real distribution cases.
# ----------------------------------------------------------------------------------------------------
DIST_CASES = [
    # name in gs_expected.csv, parent weight, extra holdings {ticker: weight}, book variant label
    ("HON_sep_short", -1.0, {}, ""),
    ("HON_sep_short", -204.0, {"WAT": 796.0}, "book"),   # GS-like book: −204/1000 HON, the rest in a real filler
    ("DD_Q_long", 1.0, {}, ""),
    ("BDX_WAT_short", -0.5, {"WAT": 0.5}, ""),       # existing long child of the opposite sign
    ("CMCSA_VSNT_long", 1.0, {}, ""),
    ("HON_SOLS_long", 1.0, {}, ""),
]
MERGER_CASES = [
    ("K_cash_short", -1.0), ("EXAS_cash_short", -1.0), ("EA_cash_long", 1.0), ("ANSS_mixed_short", -1.0),
    ("CYBR_mixed_short", -1.0), ("AVB_stock_short", -1.0), ("CTRA_stock_long", 1.0),
]


def f32(x) -> float:
    return float(np.float32(x))


def build_dist_fixture(cache: Cache, gs: pd.Series, w_parent: float, extra: dict, label: str, out: Path) -> tuple[Fixture, dict]:
    ex = pd.Timestamp(gs.ex_date)
    prev = pd.Timestamp(gs.prev_date)
    parent_t = gs.parent
    child_t = GS_CHILD_TICKER.get(gs.child, gs.child)
    parent = cache.resolve(parent_t, ex)
    child = cache.resolve(child_t, ex)
    extra_ids = {cache.resolve(t, ex): w for t, w in extra.items()}
    ids = sorted({parent, child, *extra_ids})
    cal = cache.calendar
    i_ex = int(np.flatnonzero(cal == ex)[0])
    days = cal[i_ex - 6: i_ex + 5]
    raw = cache.raw_closes(ids, days[0], days[-1])
    adj = cache.adj(raw)
    symbols = sorted(str(i) for i in ids)
    labels = {str(parent): parent_t, str(child): child_t, **{str(i): t for t, i in ((t, cache.resolve(t, ex)) for t in extra)}}
    order = [int(s) for s in symbols]
    close = adj[order].to_numpy(np.float32)
    T, N = close.shape
    sig = np.full((T, N), np.nan, dtype=np.float32)
    sig[:, symbols.index(str(parent))] = w_parent
    for i, w in extra_ids.items():
        sig[:, symbols.index(str(i))] = w
    name = gs.case + (f"_{label}" if label else "")
    fx = Fixture(name=name, dir=out / name, symbols=symbols, dates=days, close=close, sig=sig, labels=labels)
    # ledger from the §3-3 unit formulas, recomputed here from raw closes and split factors
    mask_prev = (raw.index < ex) & raw[parent].notna().to_numpy()
    prev_panel = raw.index[mask_prev][-1]
    sf_parent_prev = cache.sf(parent, prev_panel)
    sf_child_ex = cache.sf(child, ex)
    sf_parent_ex = cache.sf(parent, ex)
    ratio_cache = float(gs.childRatio) * sf_child_ex / sf_parent_prev
    cash_cache = float(gs.cashPerShare) / sf_parent_prev
    p_ex = f32(adj.at[ex, parent])
    p_before = f32(adj.at[prev_panel, parent])
    c_ex = f32(adj.at[ex, child])
    units = dict(parent=parent, child=child, prev_panel=prev_panel, sf_parent_prev=sf_parent_prev, sf_child_ex=sf_child_ex,
                 sf_parent_ex=sf_parent_ex, ratio_cache=ratio_cache, cash_cache=cash_cache, p_ex=p_ex, p_before=p_before, c_ex=c_ex,
                 raw_prev=f32(raw.at[prev_panel, parent]), raw_ex=f32(raw.at[ex, parent]), raw_child=f32(raw.at[ex, child]))
    rows = []
    if float(gs.parentFactor) != 1.0:
        rows.append(dict(tradingItemId=str(parent), exDate=ex, eventType="split", legType="quantity", cashPerShareCache=None,
                         childTradingItemId=None, childRatioCache=None, childCloseAtExCache=None, parentCloseAtExCache=p_ex,
                         parentCloseBeforeCache=p_before, parentFactorCache=1.0, payDate=ex, isEstimate=False, status="ok",
                         childValueSource="unknown", eventId=f"{parent}_{ex.date()}_split_0"))
    if float(gs.cashPerShare) > 0:
        rows.append(dict(tradingItemId=str(parent), exDate=ex, eventType="cash_dividend", legType="cash", cashPerShareCache=cash_cache,
                         childTradingItemId=None, childRatioCache=None, childCloseAtExCache=None, parentCloseAtExCache=p_ex,
                         parentCloseBeforeCache=p_before, parentFactorCache=1.0, payDate=ex, isEstimate=False, status="ok",
                         childValueSource="unknown", eventId=f"{parent}_{ex.date()}_cash_dividend_0"))
    rows.append(dict(tradingItemId=str(parent), exDate=ex, eventType="spin_off", legType="stock", cashPerShareCache=None,
                     childTradingItemId=str(child), childRatioCache=ratio_cache, childCloseAtExCache=c_ex, parentCloseAtExCache=p_ex,
                     parentCloseBeforeCache=p_before, parentFactorCache=1.0, payDate=ex, isEstimate=False, status="ok",
                     childValueSource="child_close_ex", eventId=f"{parent}_{ex.date()}_spin_off_0"))
    fx.ledger_rows = rows
    return fx, units


def ref_legs_from_rows(fx: Fixture) -> list[RefLeg]:
    legs = []
    for r in fx.ledger_rows:
        if r["legType"] == "quantity":
            continue
        bar = fx.bar(r["exDate"])
        parent = fx.idx(r["tradingItemId"])
        if r["legType"] == "cash":
            legs.append(RefLeg(bar=bar, parent=parent, stock=False, cash=math.nan if r["cashPerShareCache"] is None else float(r["cashPerShareCache"])))
        else:
            child = fx.idx(r["childTradingItemId"]) if r.get("childTradingItemId") in fx.symbols else -1
            legs.append(RefLeg(bar=bar, parent=parent, stock=True, child=child,
                               ratio=math.nan if r.get("childRatioCache") is None else float(r["childRatioCache"]),
                               child_close=math.nan if r.get("childCloseAtExCache") is None else float(r["childCloseAtExCache"]),
                               usable=r.get("childValueSource") in ("wi_close", "event_value", "vendor_implied"),
                               value=math.nan if r.get("childValuePerShareCache") is None else float(r["childValuePerShareCache"])))
    return legs


def ref_events_from_rows(fx: Fixture) -> dict[int, RefEvent]:
    events = {}
    for r in fx.event_rows:
        i = fx.idx(r["tradingItemId"])
        end = pd.Timestamp(r["securityEndDate"])
        after = np.flatnonzero(fx.dates > end + pd.Timedelta(hours=23, minutes=59, seconds=59, milliseconds=999))
        bar = int(after[0]) if len(after) else -1
        succ = fx.idx(r["successorTradingItemId"]) if r.get("successorTradingItemId") in fx.symbols else -1
        events[i] = RefEvent(bar=bar, cash=math.nan if r.get("cashPerShareCache") is None else float(r["cashPerShareCache"]),
                             ratio=math.nan if r.get("successorRatioCache") is None else float(r["successorRatioCache"]),
                             successor=succ, stock_leg=bool(r["hasStockLeg"]), cash_leg=bool(r["hasCashLeg"]))
    return events


def run_fixture(probe: Probe, fx: Fixture, res: Results, section: str, tol: float, modes=(1, 0)) -> dict:
    cfg = fx.write()
    outputs = {}
    for mode in modes:
        engine = probe.run(cfg, mode, fx.dir / f"engine_D{mode}.json")
        outputs[mode] = engine
        if "error" in engine:
            res.check(section, fx.name, f"D{mode} engine run", "ok", f"error: {engine['error']}")
            continue
        ref = reference_run(fx.close, fx.sig, engine["feeRate"], mode, fx.warmup, ref_legs_from_rows(fx), ref_events_from_rows(fx))
        outputs[f"ref{mode}"] = ref
        compare_run(res, section, fx.name, engine, ref, fx, mode, tol)
    return outputs


def strategy_weight(fx: Fixture, bar: int, idx: int, mode: int) -> float:
    """Normalized target the strategy sets for `idx` on the bar (D1 executes the previous bar's signal)."""
    row = fx.sig[bar - 1 if mode == 1 else bar].astype(np.float64)
    finite = np.isfinite(row)
    total = float(np.sum(np.abs(row[finite])))
    if total <= 1e-12 or not finite[idx]:
        return 0.0
    return float(row[idx] / total)


def other_price_pnl(fx: Fixture, ref: dict, bar: int, parent: int) -> float:
    """Reference price P&L of every asset except `parent` on `bar` (weights/marks of the previous bar)."""
    prev = ref["rows"][bar - 1]
    total = 0.0
    for i in range(len(fx.symbols)):
        if i == parent:
            continue
        w = prev["weights"][i]
        m = prev["marks"][i]
        c = float(fx.close[bar, i])
        if w != 0.0 and np.isfinite(c) and np.isfinite(m):
            total += w * (c / m - 1.0)
    return total


def section_real_distributions(args, cache: Cache, probe: Probe, res: Results, gs: pd.DataFrame, out: Path) -> list[dict]:
    rows_report = []
    for case, w_parent, extra, label in DIST_CASES:
        g = gs[gs.case == case].iloc[0]
        fx, u = build_dist_fixture(cache, g, w_parent, extra, label, out / "real")
        name = fx.name
        S = "B.real"
        # raw closes of the fixture vs the GS marks (decimal) — informational, must agree to float32 rounding
        for what, mine, theirs in (("parent prev", u["raw_prev"], g.parent_close_prev), ("parent ex", u["raw_ex"], g.parent_close_ex),
                                   ("child ex", u["raw_child"], g.child_close_ex)):
            res.check(S, name, f"cache raw close == GS mark ({what})", float(theirs), mine, tol=1e-5, kind="rel",
                      note="float32 cache close vs GS decimal mark")
        res.check(S, name, "prev_date (last observed parent close before ex) == GS prev_date", str(pd.Timestamp(g.prev_date).date()),
                  str(u["prev_panel"].date()))
        outs = run_fixture(probe, fx, res, S, args.tol)
        bar_ex = fx.bar(g.ex_date)
        p_i, c_i = fx.idx(str(u["parent"])), fx.idx(str(u["child"]))
        # oracle on the fixture's float32 marks (cache units) and on the GS decimal marks (raw units)
        ret_f32 = (u["p_ex"] + u["cash_cache"] + u["ratio_cache"] * u["c_ex"]) / u["p_before"] - 1.0
        ret_gs = (float(g.parentFactor) * float(g.parent_close_ex) + float(g.cashPerShare) + float(g.childRatio) * float(g.child_close_ex)) / float(g.parent_close_prev) - 1.0
        res.check(S, name, "gs_expected.csv ret_cash_style reproduces its own formula", float(g.ret_cash_style), ret_gs, tol=1e-12)
        entry = dict(case=name, ticker=f"{fx.labels[str(u['parent'])]}→{fx.labels[str(u['child'])]}", ex=str(pd.Timestamp(g.ex_date).date()),
                     w=w_parent, ratio_cache=u["ratio_cache"], prev_f32=u["p_before"], ex_f32=u["p_ex"], child_f32=u["c_ex"],
                     ret_f32=ret_f32, ret_gs=float(g.ret_cash_style), sf_parent_prev=u["sf_parent_prev"])
        for mode in (1, 0):
            engine = outs.get(mode)
            ref = outs.get(f"ref{mode}")
            if not engine or "error" in engine or ref is None:
                continue
            b = engine["bars"][bar_ex]
            res.check(S, name, f"D{mode} loader: one value leg scheduled, quantity row counted only", f"legs=1,quantity={1 if float(g.parentFactor) != 1.0 else 0},unpriced=0",
                      f"legs={engine['ledgerReport']['legs']},quantity={engine['ledgerReport']['rowsQuantity']},unpriced={engine['ledgerReport']['unpricedChildren']}")
            w_carried = float(engine["bars"][bar_ex - 1]["weights"][p_i])
            res.check(S, name, f"D{mode} parent weight carried into the ex-date == normalized strategy weight", strategy_weight(fx, bar_ex - 1, p_i, mode), w_carried)
            entry[f"w_carried_D{mode}"] = w_carried
            entry["w"] = w_carried
            parent_pnl = float(b["gross"]) - other_price_pnl(fx, ref, bar_ex, p_i)
            expected = w_carried * ret_f32
            res.check(S, name, f"D{mode} ex-date P&L == w × ret_cash_style (fixture float32 marks)", expected, parent_pnl, tol=args.tol)
            res.check(S, name, f"D{mode} ex-date P&L vs w × GS ret_cash_style (decimal marks; residual = float32 storage)",
                      w_carried * float(g.ret_cash_style), parent_pnl, tol=1e-6, note=f"diff {parent_pnl - w_carried * float(g.ret_cash_style):+.3e}")
            # child weight created
            recs = [d for d in engine["distributions"] if d["bar"] == bar_ex and d["parent"] == p_i]
            res.check(S, name, f"D{mode} one distribution record on the ex-date", 1, len(recs))
            if recs:
                cw_expected = u["ratio_cache"] * w_carried * u["c_ex"] / u["p_before"]
                res.check(S, name, f"D{mode} child weight == ratioCache × w × child_close / mark", cw_expected, float(recs[0]["childWeight"]), tol=args.tol)
                res.check(S, name, f"D{mode} record source", "childClose", recs[0]["source"])
                entry[f"child_w_D{mode}"] = float(recs[0]["childWeight"])
                # book weight after the ex bar = strategy target + received (netting with an existing holding)
                w_child_after = float(b["weights"][c_i])
                strategy_child = strategy_weight(fx, bar_ex, c_i, mode)
                res.check(S, name, f"D{mode} child book weight after the ex bar == strategy + received", strategy_child + float(recs[0]["childWeight"]), w_child_after, tol=args.tol)
            # next-bar liquidation of the received weight (target 0) or restoration of the strategy target
            nb = engine["bars"][bar_ex + 1]
            trade_child = float(nb["trades"][c_i])
            received = float(recs[0]["childWeight"]) if recs else 0.0
            res.check(S, name, f"D{mode} next-bar child trade == −received weight", -received, trade_child, tol=args.tol)
            res.check(S, name, f"D{mode} next-bar fee == fee_rate × turnover", float(nb["turnover"]) * engine["feeRate"], float(nb["fee"]), tol=1e-15)
            res.check(S, name, f"D{mode} ex-date turnover == 0 (received weight is not a trade)", 0.0, float(b["turnover"]), tol=args.tol)
            entry[f"engine_D{mode}"] = parent_pnl
            entry[f"diff_f32_D{mode}"] = parent_pnl - expected
            entry[f"diff_gs_D{mode}"] = parent_pnl - w_carried * float(g.ret_cash_style)
        entry["units"] = u
        entry["fixture"] = fx
        rows_report.append(entry)
    res.tables["real_dist"] = rows_report
    return rows_report


def build_merger_fixture(cache: Cache, gs: pd.Series, w_parent: float, out: Path) -> tuple[Fixture, dict]:
    ex_gs = pd.Timestamp(gs.ex_date)
    prev_gs = pd.Timestamp(gs.prev_date)
    parent_t = gs.parent
    parent = cache.resolve(parent_t, prev_gs)
    child_t = None if pd.isna(gs.child) or gs.child == "" else GS_CHILD_TICKER.get(gs.child, gs.child)
    child = cache.resolve(child_t, ex_gs) if child_t else None
    ids = sorted({parent, *( [child] if child else [] )})
    cal = cache.calendar
    i_ex = int(np.flatnonzero(cal == ex_gs)[0])
    days = cal[i_ex - 7: i_ex + 4]
    raw = cache.raw_closes(ids, days[0], days[-1])
    adj = cache.adj(raw)
    symbols = sorted(str(i) for i in ids)
    labels = {str(parent): parent_t}
    if child:
        labels[str(child)] = child_t
    order = [int(s) for s in symbols]
    close = adj[order].to_numpy(np.float32)
    T, N = close.shape
    sig = np.full((T, N), np.nan, dtype=np.float32)
    sig[:, symbols.index(str(parent))] = w_parent
    fx = Fixture(name=gs.case, dir=out / gs.case, symbols=symbols, dates=days, close=close, sig=sig, labels=labels)
    # events row: end date = the parent's last observed close in the cache (the pipeline's convention),
    # the settlement opens on the next bar and closes on the first bar without a parent quote.
    last_obs = raw.index[raw[parent].notna()][-1]
    later = days[days > last_obs]
    settle = next(d for d in later if not np.isfinite(raw.at[d, parent]))
    sf_parent_mark = cache.sf(parent, last_obs)
    cash = float(gs.cashPerShare) if float(gs.cashPerShare) > 0 else None
    ratio = float(gs.childRatio) if child and float(gs.childRatio) > 0 else None
    row = dict(tradingItemId=str(parent), securityEndDate=last_obs, cashPerShareCache=None if cash is None else cash / sf_parent_mark,
               successorTradingItemId=str(child) if ratio else None,
               successorRatioCache=None if ratio is None else ratio * cache.sf(child, settle) / sf_parent_mark,
               hasStockLeg=ratio is not None, hasCashLeg=cash is not None,
               successorCacheClose=f32(adj.at[settle, child]) if ratio else None, successorCacheCloseDate=settle if ratio else None,
               targetCacheMark=f32(adj.at[last_obs, parent]), targetCacheMarkDate=last_obs, settlementReturn=None)
    fx.event_rows = [row]
    units = dict(parent=parent, child=child, last_obs=last_obs, settle=settle, mark=f32(adj.at[last_obs, parent]),
                 succ_close=f32(adj.at[settle, child]) if ratio else math.nan, cash_cache=row["cashPerShareCache"], ratio_cache=row["successorRatioCache"],
                 succ_close_gs_date=f32(adj.at[ex_gs, child]) if (ratio and ex_gs in adj.index and np.isfinite(adj.at[ex_gs, child])) else math.nan)
    return fx, units


def section_real_mergers(args, cache: Cache, probe: Probe, res: Results, gs: pd.DataFrame, out: Path) -> list[dict]:
    S = "B.merger"
    rows_report = []
    for case, w_parent in MERGER_CASES:
        g = gs[gs.case == case].iloc[0]
        fx, u = build_merger_fixture(cache, g, w_parent, out / "real")
        outs = run_fixture(probe, fx, res, S, args.tol)
        p_i = fx.idx(str(u["parent"]))
        c_i = fx.idx(str(u["child"])) if u["child"] else -1
        bar_settle = fx.bar(u["settle"])
        cash = 0.0 if u["cash_cache"] is None else u["cash_cache"]
        ratio = 0.0 if u["ratio_cache"] is None else u["ratio_cache"]
        R_engine_expected = (cash + ratio * (u["succ_close"] if ratio else 0.0)) / u["mark"] - 1.0
        R_gs = float(g.ret_cash_style)
        res.check(S, case, "cache mark == GS parent_close_prev", float(g.parent_close_prev), u["mark"], tol=1e-5, kind="rel")
        entry = dict(case=case, ticker=fx.labels[str(u["parent"])] + (f"→{fx.labels[str(u['child'])]}" if u["child"] else ""),
                     w=w_parent, end_date=str(u["last_obs"].date()), settle=str(u["settle"].date()), gs_ex=str(pd.Timestamp(g.ex_date).date()),
                     mark=u["mark"], cash=cash, ratio=ratio, succ_close=u["succ_close"], succ_close_gs_date=u["succ_close_gs_date"],
                     R_expected=R_engine_expected, R_gs=R_gs)
        for mode in (1, 0):
            engine = outs.get(mode)
            if not engine or "error" in engine:
                continue
            b = engine["bars"][bar_settle]
            w_carried = float(engine["bars"][bar_settle - 1]["weights"][p_i])
            res.check(S, case, f"D{mode} parent weight carried into the settlement bar", w_parent, w_carried)
            recs = [s for s in engine["settlements"] if s["asset"] == p_i]
            res.check(S, case, f"D{mode} one settlement record", 1, len(recs))
            if recs:
                r = recs[0]
                res.check(S, case, f"D{mode} settlement bar == first bar after the end date without a parent quote", bar_settle, int(r["bar"]))
                res.check(S, case, f"D{mode} settlement source computed", "computed", r["source"])
                res.check(S, case, f"D{mode} settlement return == (cash + ratio × close_succ(settle))/mark − 1", R_engine_expected, float(r["return"]), tol=args.tol)
                res.check(S, case, f"D{mode} settlement P&L == w × R", w_carried * R_engine_expected, float(b["missingData"]["delistingPnl"]), tol=args.tol)
                res.check(S, case, f"D{mode} settlement bar gross == settlement P&L (nothing else held)", float(b["missingData"]["delistingPnl"]), float(b["gross"]), tol=args.tol)
                if ratio:
                    transfer_expected = w_carried * ratio * u["succ_close"] / u["mark"]
                    res.check(S, case, f"D{mode} stock leg transferred to the successor == w × ratio × close_succ / mark", transfer_expected, float(r["transferred"]), tol=args.tol)
                    # events path (pre-existing Phase 2 rule): the transferred weight lands before the bar's execution,
                    # so the same bar rebalances it to the strategy target (0 here) with the normal fee —
                    # unlike spin-off children, which join the book after the execution and trade the next bar.
                    res.check(S, case, f"D{mode} settlement-bar trade in the successor == −transferred (rebalanced to target 0 on the same bar)", -transfer_expected, float(b["trades"][c_i]), tol=args.tol)
                    res.check(S, case, f"D{mode} settlement-bar turnover == |transferred| (the settlement itself is not a trade)", abs(transfer_expected), float(b["turnover"]), tol=args.tol)
                    res.check(S, case, f"D{mode} settlement-bar fee == fee_rate × |transferred|", engine["feeRate"] * abs(transfer_expected), float(b["fee"]), tol=1e-12)
                    res.check(S, case, f"D{mode} successor book weight after the settlement bar == strategy target 0", 0.0, float(b["weights"][c_i]), tol=args.tol)
                    nb = engine["bars"][bar_settle + 1]
                    res.check(S, case, f"D{mode} next bar: nothing left to trade in the successor", 0.0, float(nb["trades"][c_i]), tol=args.tol)
                else:
                    res.check(S, case, f"D{mode} cash settlement carries no turnover", 0.0, float(b["turnover"]), tol=args.tol)
                entry[f"R_engine_D{mode}"] = float(r["return"])
                entry[f"pnl_engine_D{mode}"] = float(b["missingData"]["delistingPnl"])
                same_date = entry["settle"] == entry["gs_ex"]
                diff_gs = float(r["return"]) - R_gs
                entry[f"diff_gs_D{mode}"] = diff_gs
                if ratio and not same_date:
                    res.info(S, case, f"D{mode} vs GS: acquirer priced on {entry['settle']} (engine settlement bar) instead of {entry['gs_ex']} (GS)",
                             f"R_engine {float(r['return']):+.9f} vs R_gs {R_gs:+.9f} (diff {diff_gs:+.3e}); "
                             f"successor close {u['succ_close']:.6f} vs {u['succ_close_gs_date']:.6f} on the GS date",
                             note="price-date convention, reported not fixed")
                else:
                    res.check(S, case, f"D{mode} settlement return vs GS ret_cash_style (same acquirer date / cash only; residual = float32 storage)",
                              R_gs, float(r["return"]), tol=1e-6, note=f"diff {diff_gs:+.3e}")
        entry["fixture"] = fx
        rows_report.append(entry)
        # Variant: the production events row (rc1 events/corporate_actions.parquet, k-anchored cache units) instead of
        # the row built here with k = 1. The anchoring multiplies cash/ratio by the same float32 rounding as the mark,
        # so a cash deal reproduces the GS decimal return to double precision.
        rc1 = cache.root / "events" / "corporate_actions.parquet"
        if rc1.exists():
            ev = pq.read_table(rc1).to_pandas()
            row = ev[(ev.tradingItemId == u["parent"])]
            if len(row) == 1 and row.iloc[0].cacheUnitStatus == "ok":
                r0 = row.iloc[0]
                fx2 = Fixture(name=case + "_rc1events", dir=out / "real" / (case + "_rc1events"), symbols=fx.symbols, dates=fx.dates, close=fx.close,
                              sig=fx.sig, labels=fx.labels)
                succ = str(int(r0.successorTradingItemId)) if pd.notna(r0.successorTradingItemId) else None
                fx2.event_rows = [dict(tradingItemId=str(u["parent"]), securityEndDate=pd.Timestamp(r0.endDate),
                                       cashPerShareCache=None if pd.isna(r0.cashPerShareCache) else float(r0.cashPerShareCache),
                                       successorTradingItemId=succ if succ in fx.symbols else None,
                                       successorRatioCache=None if pd.isna(r0.successorRatioCache) else float(r0.successorRatioCache),
                                       hasStockLeg=bool(r0.hasStockLeg), hasCashLeg=bool(r0.hasCashLeg),
                                       successorCacheClose=None if pd.isna(r0.successorCacheClose) else float(r0.successorCacheClose),
                                       successorCacheCloseDate=None if pd.isna(r0.successorCacheCloseDate) else pd.Timestamp(r0.successorCacheCloseDate),
                                       targetCacheMark=None if pd.isna(r0.targetCacheMark) else float(r0.targetCacheMark),
                                       targetCacheMarkDate=None if pd.isna(r0.targetCacheMarkDate) else pd.Timestamp(r0.targetCacheMarkDate),
                                       settlementReturn=None)]
                outs2 = run_fixture(probe, fx2, res, S, args.tol)
                for mode in (1, 0):
                    e2 = outs2.get(mode)
                    if not e2 or "error" in e2:
                        continue
                    recs = [x for x in e2["settlements"] if x["asset"] == p_i]
                    if not recs:
                        res.check(S, fx2.name, f"D{mode} settlement record", 1, 0)
                        continue
                    R2 = float(recs[0]["return"])
                    same_date = fx2.dates[int(recs[0]["bar"])] == pd.Timestamp(g.ex_date)
                    res.info(S, fx2.name, f"D{mode} rc1 events row", f"endDate {pd.Timestamp(r0.endDate).date()}, cashCache {r0.cashPerShareCache}, ratioCache {r0.successorRatioCache}, "
                             f"settled {fx2.dates[int(recs[0]['bar'])].date()}, R {R2:+.12f} (k = 1 row: R {entry.get(f'R_engine_D{mode}', math.nan):+.12f}, GS {R_gs:+.12f})")
                    if not ratio:
                        res.check(S, fx2.name, f"D{mode} k-anchored rc1 cash terms reproduce the GS decimal return exactly", R_gs, R2, tol=1e-9,
                                  note="k = as-traded / cache close cancels the float32 rounding of the mark")
                    elif same_date:
                        res.check(S, fx2.name, f"D{mode} k-anchored rc1 stock terms reproduce the GS decimal return (residual = generator's as-traded inputs)", R_gs, R2, tol=1e-7,
                                  note=f"diff {R2 - R_gs:+.3e}; cacheUnitFactorTarget/Successor come from DB as-traded prices, not the GS marks")
                    entry[f"R_rc1_D{mode}"] = R2
            else:
                res.info(S, case, "rc1 events row", f"{len(row)} row(s), cacheUnitStatus {row.iloc[0].cacheUnitStatus if len(row) else 'n/a'} — k-anchored variant skipped")
    res.tables["real_merger"] = rows_report
    return rows_report


# ----------------------------------------------------------------------------------------------------
# C. Synthetic cases.
# ----------------------------------------------------------------------------------------------------
def synth_days(n: int, start="2025-01-06") -> pd.DatetimeIndex:
    return pd.bdate_range(start, periods=n)


def synth_fixture(name: str, out: Path, closes: dict, sigs: dict, ledger: list[dict] = (), events: list[dict] = (),
                  warmup: int = 0, stale_check: bool = True) -> Fixture:
    symbols = sorted(closes)
    T = len(next(iter(closes.values())))
    days = synth_days(T)
    close = np.array([[closes[s][t] for s in symbols] for t in range(T)], dtype=np.float32)
    sig = np.array([[sigs.get(s, [np.nan] * T)[t] for s in symbols] for t in range(T)], dtype=np.float32)
    fx = Fixture(name=name, dir=out / "synthetic" / name, symbols=symbols, dates=days, close=close, sig=sig,
                 ledger_rows=list(ledger), event_rows=list(events), warmup=warmup, labels={s: s for s in symbols}, stale_check=stale_check)
    return fx


def L(parent, day_index, days, kind, cash=None, child=None, ratio=None, child_close=None, p_ex=None, p_before=None,
      source="child_close_ex", estimate=False, status="ok", seq=0, marker=None, value=None, reason=None):
    ex = days[day_index]
    leg = "cash" if kind in ("cash_dividend", "special_dividend") else ("quantity" if kind == "split" else "stock")
    return dict(tradingItemId=parent, exDate=ex, eventType=kind, legType=leg, cashPerShareCache=cash, childTradingItemId=child,
                childRatioCache=ratio, childCloseAtExCache=child_close, parentCloseAtExCache=p_ex, parentCloseBeforeCache=p_before,
                parentFactorCache=1.0, payDate=ex, isEstimate=estimate, status="unresolved" if marker else status, childValueSource=source,
                eventId=f"{parent}_{ex.date()}_{kind}_{seq}", markerReason=marker, childValuePerShareCache=value, estimateReason=reason)


def section_synthetic(args, probe: Probe, res: Results, out: Path):
    S = "C.synthetic"
    nan = np.nan
    days = synth_days(8)
    summary = []

    def run_case(fx: Fixture, closed_form):
        outs = run_fixture(probe, fx, res, S, args.tol)
        for mode in (1, 0):
            engine = outs.get(mode)
            if engine and "error" not in engine:
                closed_form(mode, engine)
        summary.append(fx.name)

    # 1. long dividend: 100 → 95 + cash 5 = 0 on the ex bar; dividend P&L 0.05; no quantity change, no turnover
    fx = synth_fixture("long_dividend", out, {"P": [100, 100, 100, 95, 95, 95, 95, 95]}, {"P": [1] * 8},
                       [L("P", 3, days, "cash_dividend", cash=5.0, p_ex=95.0, p_before=100.0)])
    def cf(mode, e):
        b = e["bars"][3]
        res.check(S, fx.name, f"D{mode} ex-bar gross == w × ((95 + 5)/100 − 1) = 0", 0.0, float(b["gross"]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} dividendPnl == 1 × 5/100", 0.05, float(b["corporateActions"]["dividendPnl"]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} ex-bar turnover 0 (cash changes no quantity)", 0.0, float(b["turnover"]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} weight unchanged", 1.0, float(b["weights"][0]), tol=args.tol)
    run_case(fx, cf)

    # 2. short dividend: the short pays the dividend (−0.05) and gains the price drop (+0.05)
    fx = synth_fixture("short_dividend", out, {"P": [100, 100, 100, 95, 95, 95, 95, 95]}, {"P": [-1] * 8},
                       [L("P", 3, days, "cash_dividend", cash=5.0, p_ex=95.0, p_before=100.0)])
    def cf(mode, e):
        b = e["bars"][3]
        res.check(S, fx.name, f"D{mode} ex-bar gross == −1 × ((95 + 5)/100 − 1) = 0", 0.0, float(b["gross"]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} dividendPnl == −1 × 5/100", -0.05, float(b["corporateActions"]["dividendPnl"]), tol=args.tol)
    run_case(fx, cf)

    # 3. spin long: parent 100 → 80, child 40 × 0.5 = 20 → cash-style 0; child weight 0.2; liquidated next bar at 42 with fee
    fx = synth_fixture("spin_long", out, {"P": [100, 100, 100, 80, 80, 80, 80, 80], "C": [nan, nan, nan, 40, 42, 42, 42, 42]}, {"P": [1] * 8},
                       [L("P", 3, days, "spin_off", child="C", ratio=0.5, child_close=40.0, p_ex=80.0, p_before=100.0)])
    def cf(mode, e):
        b, nb = e["bars"][3], e["bars"][4]
        res.check(S, fx.name, f"D{mode} ex-bar gross == (80 + 0.5×40)/100 − 1 = 0", 0.0, float(b["gross"]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} distributionPnl == 1 × 0.5 × 40/100", 0.2, float(b["corporateActions"]["distributionPnl"]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} child weight after ex bar", 0.2, float(b["weights"][0]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} ex-bar turnover 0", 0.0, float(b["turnover"]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} next-bar gross == 0.2 × (42/40 − 1)", 0.2 * (42 / 40 - 1), float(nb["gross"]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} next-bar turnover == |child weight| 0.2", 0.2, float(nb["turnover"]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} next-bar fee == fee_rate × 0.2", e["feeRate"] * 0.2, float(nb["fee"]), tol=1e-15)
        res.check(S, fx.name, f"D{mode} next-bar ret == gross − fee", float(nb["gross"]) - e["feeRate"] * 0.2, float(nb["ret"]), tol=1e-15)
        res.check(S, fx.name, f"D{mode} child weight liquidated", 0.0, float(nb["weights"][0]), tol=args.tol)
    run_case(fx, cf)

    # 4. spin short: child weight −0.2, bought back next bar
    fx = synth_fixture("spin_short", out, {"P": [100, 100, 100, 80, 80, 80, 80, 80], "C": [nan, nan, nan, 40, 42, 42, 42, 42]}, {"P": [-1] * 8},
                       [L("P", 3, days, "spin_off", child="C", ratio=0.5, child_close=40.0, p_ex=80.0, p_before=100.0)])
    def cf(mode, e):
        b, nb = e["bars"][3], e["bars"][4]
        res.check(S, fx.name, f"D{mode} ex-bar gross == 0", 0.0, float(b["gross"]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} child weight == −0.2", -0.2, float(b["weights"][0]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} next-bar gross == −0.2 × (42/40 − 1)", -0.2 * (42 / 40 - 1), float(nb["gross"]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} next-bar turnover 0.2 (buy-back)", 0.2, float(nb["turnover"]), tol=args.tol)
    run_case(fx, cf)

    # 5. existing child of the opposite sign: parent +0.6, strategy child −0.4, received +0.12 → −0.28, restored to −0.4 next bar
    fx = synth_fixture("existing_child_netting", out, {"P": [100, 100, 100, 80, 80, 80, 80, 80], "C": [40, 40, 40, 40, 40, 40, 40, 40]},
                       {"P": [0.75] * 8, "C": [-0.25] * 8},
                       [L("P", 3, days, "spin_off", child="C", ratio=0.5, child_close=40.0, p_ex=80.0, p_before=100.0)])
    def cf(mode, e):
        b, nb = e["bars"][3], e["bars"][4]
        res.check(S, fx.name, f"D{mode} received == 0.75 × 0.5 × 40/100", 0.15, float(b["corporateActions"]["distributionPnl"]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} child book weight == −0.25 + 0.15 (netted, sign preserved)", -0.10, float(b["weights"][0]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} ex-bar gross == 0.75 × (80/100 − 1) + 0.15 + 0 (child flat)", 0.75 * (80 / 100 - 1) + 0.15, float(b["gross"]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} next-bar child trade == −0.15 (back to the −0.25 target)", -0.15, float(nb["trades"][0]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} next-bar turnover == 0.15", 0.15, float(nb["turnover"]), tol=args.tol)
    run_case(fx, cf)

    # 6. fractional ratio 1/3 with a fractional weight
    fx = synth_fixture("fractional_ratio", out, {"P": [90, 90, 90, 75, 75, 75, 75, 75], "C": [nan, nan, nan, 33.3, 33.3, 33.3, 33.3, 33.3], "F": [50] * 8},
                       {"P": [0.37] * 8, "F": [0.63] * 8},
                       [L("P", 3, days, "spin_off", child="C", ratio=1 / 3, child_close=f32(33.3), p_ex=75.0, p_before=90.0)])
    def cf(mode, e):
        b = e["bars"][3]
        w = strategy_weight(fx, 2, fx.idx("P"), mode)      # float32 signal 0.37 / (0.37 + 0.63)
        expected = w * (1 / 3) * f32(33.3) / 90.0
        res.check(S, fx.name, f"D{mode} child weight == w × (1/3) × 33.3/90 (w = f32-normalized 0.37)", expected, float(b["weights"][0]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} ex-bar gross == w × ((75 + 33.3/3)/90 − 1)", w * ((75.0 + f32(33.3) / 3) / 90.0 - 1), float(b["gross"]), tol=args.tol)
    run_case(fx, cf)

    # 7. halted child: no quote on the ex-date (and the day after), first quote two bars later at 44 → pending then firstQuote
    fx = synth_fixture("halted_child_pending", out, {"P": [100, 100, 100, 80, 82, 84, 84, 84], "C": [nan, nan, nan, nan, nan, 44, 45, 45]}, {"P": [1] * 8},
                       [L("P", 3, days, "spin_off", child="C", ratio=0.5, child_close=None, p_ex=80.0, p_before=100.0, source="unknown")])
    def cf(mode, e):
        b3, b4, b5, b6 = e["bars"][3], e["bars"][4], e["bars"][5], e["bars"][6]
        res.check(S, fx.name, f"D{mode} ex-bar: nothing booked for the child, parent move only", 80 / 100 - 1, float(b3["gross"]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} ex-bar pending == 1", 1, int(b3["corporateActions"]["distributionPending"]))
        res.check(S, fx.name, f"D{mode} bar 4 still pending", 1, int(b4["corporateActions"]["distributionPending"]))
        res.check(S, fx.name, f"D{mode} first-quote bar: child weight == w_ex × 0.5 × 44 / m_ex(100)", 0.5 * 44 / 100, float(b5["corporateActions"]["distributionPnl"]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} first-quote bar gross == parent move + received", (84 / 82 - 1) + 0.5 * 44 / 100, float(b5["gross"]), tol=args.tol)
        recs = [d for d in e["distributions"] if d["parent"] == 1]
        res.check(S, fx.name, f"D{mode} record source firstQuote on bar 5 with exBar 3", "firstQuote@5/3", f"{recs[0]['source']}@{recs[0]['bar']}/{recs[0]['exBar']}" if recs else "none")
        res.check(S, fx.name, f"D{mode} liquidated next bar at 45", 0.22 * (45 / 44 - 1), float(b6["gross"]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} no future price pulled to the ex-date (ex-bar distributionPnl 0)", 0.0, float(b3["corporateActions"]["distributionPnl"]), tol=args.tol)
    run_case(fx, cf)

    # 8. child not in the panel: unpriced, nothing recognised
    fx = synth_fixture("child_not_in_panel", out, {"P": [100, 100, 100, 80, 80, 80, 80, 80]}, {"P": [1] * 8},
                       [L("P", 3, days, "spin_off", child="Z", ratio=0.5, child_close=None, p_ex=80.0, p_before=100.0, source="unknown")])
    def cf(mode, e):
        b = e["bars"][3]
        res.check(S, fx.name, f"D{mode} unpricedChildren reported by the loader", 1, int(e["ledgerReport"]["unpricedChildren"]))
        res.check(S, fx.name, f"D{mode} distributionUnpriced == 1", 1, int(b["corporateActions"]["distributionUnpriced"]))
        res.check(S, fx.name, f"D{mode} ex-bar gross == parent move only (−0.2)", -0.2, float(b["gross"]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} no child position created", 0, int(b["corporateActions"]["childPositionsCreated"]))
    run_case(fx, cf)

    # 9. same-day cash + stock as two ledger rows: 100 → 78 + cash 2 + 0.5 × 40 = 0
    fx = synth_fixture("same_day_cash_and_stock", out, {"P": [100, 100, 100, 78, 78, 78, 78, 78], "C": [nan, nan, nan, 40, 40, 40, 40, 40]}, {"P": [1] * 8},
                       [L("P", 3, days, "spin_off", child="C", ratio=0.5, child_close=40.0, p_ex=78.0, p_before=100.0, seq=1),
                        L("P", 3, days, "cash_dividend", cash=2.0, p_ex=78.0, p_before=100.0, seq=0)])
    def cf(mode, e):
        b = e["bars"][3]
        res.check(S, fx.name, f"D{mode} ex-bar gross == (78 + 2 + 20)/100 − 1 = 0", 0.0, float(b["gross"]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} dividendPnl == 2/100", 0.02, float(b["corporateActions"]["dividendPnl"]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} distributionPnl == 0.5 × 40/100", 0.2, float(b["corporateActions"]["distributionPnl"]), tol=args.tol)
        recs = [d for d in e["distributions"]]
        res.check(S, fx.name, f"D{mode} record cashShare == 2/100 (cash booked beside the stock leg)", 0.02, float(recs[0]["cashShare"]) if recs else nan, tol=args.tol)
        res.check(S, fx.name, f"D{mode} two legs loaded", 2, int(e["ledgerReport"]["legs"]))
    run_case(fx, cf)

    # 10. reverse split + spin (HON-like): raw 200 → 2 old = 1 new at 210 + 0.5 child (190) per old share.
    #     split_adj basis: prev 400, ex 210, ratioCache 0.5 × 1/0.5 = 1.0 → (210 + 190)/400 − 1 = 0 == raw (0.5×210 + 0.5×190)/200 − 1
    fx = synth_fixture("reverse_split_plus_spin", out, {"P": [400, 400, 400, 210, 210, 210, 210, 210], "C": [nan, nan, nan, 190, 190, 190, 190, 190]}, {"P": [-1] * 8},
                       [L("P", 3, days, "split", p_ex=210.0, p_before=400.0, source="unknown", seq=0),
                        L("P", 3, days, "spin_off", child="C", ratio=1.0, child_close=190.0, p_ex=210.0, p_before=400.0, seq=1)])
    def cf(mode, e):
        b = e["bars"][3]
        raw_identity = (0.5 * 210 + 0.5 * 190) / 200 - 1
        res.check(S, fx.name, f"D{mode} ex-bar gross == −1 × ((210 + 1.0×190)/400 − 1) == raw-basis identity", -raw_identity, float(b["gross"]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} child weight == −1 × 1.0 × 190/400", -0.475, float(b["weights"][0]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} quantity row counted, not booked", 1, int(e["ledgerReport"]["rowsQuantity"]))
    run_case(fx, cf)

    # 11. child with a nonzero strategy target (P8): target 0.3 from the ex-date, received 0.14 on top, back to 0.3 next bar
    fx = synth_fixture("child_strategy_target", out, {"P": [100, 100, 100, 100, 100, 100, 100, 100], "C": [50] * 8},
                       {"P": [1, 1, 1, 0.75, 0.75, 0.75, 0.75, 0.75], "C": [nan, nan, nan, 0.25, 0.25, 0.25, 0.25, 0.25]},
                       [L("P", 3, days, "spin_off", child="C", ratio=0.4, child_close=50.0, p_ex=100.0, p_before=100.0)])
    def cf(mode, e):
        b, nb = e["bars"][3], e["bars"][4]
        if mode == 0:
            res.check(S, fx.name, "D0 ex-bar: strategy child target 0.25 executed + received 1×0.4×50/100 = 0.2 → 0.45", 0.45, float(b["weights"][0]), tol=args.tol)
            res.check(S, fx.name, "D0 ex-bar trade in the child is the strategy's 0.25 only", 0.25, float(b["trades"][0]), tol=args.tol)
            res.check(S, fx.name, "D0 next bar back to the 0.25 target (−0.2)", -0.2, float(nb["trades"][0]), tol=args.tol)
        else:
            res.check(S, fx.name, "D1 ex-bar: yesterday's target (child NaN → 0) + received 0.2", 0.2, float(b["weights"][0]), tol=args.tol)
            res.check(S, fx.name, "D1 next bar: target 0.25 executed (+0.05)", 0.05, float(nb["trades"][0]), tol=args.tol)
            res.check(S, fx.name, "D1 next bar child weight 0.25 (kept at the strategy target)", 0.25, float(nb["weights"][0]), tol=args.tol)
    run_case(fx, cf)

    # 12. warmup bar: a leg during warmup books nothing (no holdings exist)
    fx = synth_fixture("warmup_bar", out, {"P": [100, 100, 95, 95, 95, 95, 95, 95]}, {"P": [1] * 8},
                       [L("P", 2, days, "cash_dividend", cash=5.0, p_ex=95.0, p_before=100.0)], warmup=3)
    def cf(mode, e):
        b = e["bars"][2]
        res.check(S, fx.name, f"D{mode} warmup ex-bar books nothing", 0, int(b["corporateActions"]["dividendCount"]))
        res.check(S, fx.name, f"D{mode} warmup gross 0", 0.0, float(b["gross"]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} legs still loaded/scheduled", 1, int(e["ledgerReport"]["legs"]))
        res.check(S, fx.name, f"D{mode} trading starts at bar 3 with no dividend", 0.0, float(e["bars"][3]["corporateActions"]["dividendPnl"]), tol=args.tol)
    run_case(fx, cf)

    # 13. D1: the parent is sold on the ex-date close (signal 0 the bar before) — the entitlement uses the carried weight
    fx = synth_fixture("sold_on_ex_date", out, {"P": [100, 100, 100, 95, 95, 95, 95, 95]}, {"P": [1, 1, 0, 0, 0, 0, 0, 0]},
                       [L("P", 3, days, "cash_dividend", cash=5.0, p_ex=95.0, p_before=100.0)])
    def cf(mode, e):
        b = e["bars"][3]
        if mode == 1:
            res.check(S, fx.name, "D1 carried weight 1 on the ex-date → dividend 0.05", 0.05, float(b["corporateActions"]["dividendPnl"]), tol=args.tol)
            res.check(S, fx.name, "D1 then sold at the ex-date close (turnover 1)", 1.0, float(b["turnover"]), tol=args.tol)
            res.check(S, fx.name, "D1 ex-bar gross == 0 (−0.05 price + 0.05 cash)", 0.0, float(b["gross"]), tol=args.tol)
        else:
            res.check(S, fx.name, "D0 sold at bar 2 close → nothing carried into the ex-date, no dividend", 0.0, float(b["corporateActions"]["dividendPnl"]), tol=args.tol)
    run_case(fx, cf)

    # 14. bought on the ex-date close: no entitlement
    fx = synth_fixture("bought_on_ex_date", out, {"P": [100, 100, 100, 95, 95, 95, 95, 95]}, {"P": [nan, nan, 1, 1, 1, 1, 1, 1]},
                       [L("P", 3, days, "cash_dividend", cash=5.0, p_ex=95.0, p_before=100.0)])
    def cf(mode, e):
        b = e["bars"][3]
        if mode == 1:
            res.check(S, fx.name, "D1 bought at the ex-date close (signal at bar 2): no dividend", 0.0, float(b["corporateActions"]["dividendPnl"]), tol=args.tol)
        else:
            res.check(S, fx.name, "D0 bought at bar 2 close: entitled, dividend 0.05", 0.05, float(b["corporateActions"]["dividendPnl"]), tol=args.tol)
    run_case(fx, cf)

    # 15. parent frozen (no quote) on the ex-date: cash booked against the last mark, the price move on resumption
    fx = synth_fixture("parent_frozen_on_ex_date", out, {"P": [100, 100, 100, nan, 95, 95, 95, 95]}, {"P": [1] * 8},
                       [L("P", 3, days, "cash_dividend", cash=5.0, p_ex=None, p_before=100.0)])
    def cf(mode, e):
        b, nb = e["bars"][3], e["bars"][4]
        res.check(S, fx.name, f"D{mode} ex-bar gross == cash only 0.05 (position frozen)", 0.05, float(b["gross"]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} resume bar gross == 95/100 − 1", -0.05, float(nb["gross"]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} frozen gross on the ex bar", 1.0, float(b["missingData"]["frozenGross"]), tol=args.tol)
    run_case(fx, cf)

    # 16. ledger stale against the cache: wrong parentCloseBeforeCache → the loader must refuse the start
    fx = synth_fixture("stale_ledger_refused", out, {"P": [100, 100, 100, 95, 95, 95, 95, 95]}, {"P": [1] * 8},
                       [L("P", 3, days, "cash_dividend", cash=5.0, p_ex=95.0, p_before=101.0)])
    cfg = fx.write()
    engine = probe.run(cfg, 1, fx.dir / "engine_D1.json")
    res.check(S, fx.name, "loader refuses a stale ledger (parentCloseBeforeCache 101 vs cache 100)", True, "error" in engine and "stale" in engine["error"])
    res.info(S, fx.name, "refusal message", engine.get("error", "")[:160])
    summary.append(fx.name)

    # 17. ledger value (when-issued) for a child that never quoted in the panel: booked from the ledger, first quote earns the move
    fx = synth_fixture("ledger_value_when_issued", out, {"P": [100, 100, 100, 80, 80, 80, 80, 80], "C": [nan, nan, nan, nan, 44, 44, 44, 44]}, {"P": [1] * 8},
                       [L("P", 3, days, "spin_off", child="C", ratio=0.5, child_close=40.0, p_ex=80.0, p_before=100.0, source="wi_close")])
    def cf(mode, e):
        b, nb = e["bars"][3], e["bars"][4]
        res.check(S, fx.name, f"D{mode} ex-bar child weight from the W/I value 0.5×40/100", 0.2, float(b["corporateActions"]["distributionPnl"]), tol=args.tol)
        recs = e["distributions"]
        res.check(S, fx.name, f"D{mode} record source ledgerValue, estimate false (P10 ②)", "ledgerValue/False", f"{recs[0]['source']}/{recs[0]['estimate']}" if recs else "none")
        res.check(S, fx.name, f"D{mode} first quote 44 earns 0.2 × (44/40 − 1) then liquidates", 0.2 * (44 / 40 - 1), float(nb["gross"]), tol=args.tol)
    run_case(fx, cf)

    # 18. mixed merger through events: cash 10 + 0.5 successor (quoted at 150) on the settlement bar; transferred stock leg only
    ev_days = synth_days(8)
    fx = synth_fixture("events_mixed_merger", out, {"P": [100, 100, 100, 100, nan, nan, nan, nan], "S": [140, 140, 140, 145, 150, 152, 152, 152]}, {"P": [-1] * 8},
                       events=[dict(tradingItemId="P", securityEndDate=ev_days[3], cashPerShareCache=10.0, successorTradingItemId="S", successorRatioCache=0.5,
                                    hasStockLeg=True, hasCashLeg=True, successorCacheClose=150.0, successorCacheCloseDate=ev_days[4],
                                    targetCacheMark=100.0, targetCacheMarkDate=ev_days[3], settlementReturn=None)])
    def cf(mode, e):
        b, nb = e["bars"][4], e["bars"][5]
        R = (10 + 0.5 * 150) / 100 - 1
        res.check(S, fx.name, f"D{mode} settlement return (10 + 0.5×150)/100 − 1", R, float(b["missingData"]["delistingPnl"]) / -1.0, tol=args.tol)
        recs = e["settlements"]
        res.check(S, fx.name, f"D{mode} transferred == −1 × 0.5 × 150/100", -0.75, float(recs[0]["transferred"]) if recs else nan, tol=args.tol)
        res.check(S, fx.name, f"D{mode} settlement bar: transferred weight rebalanced to target 0 on the same bar (turnover 0.75)", 0.75, float(b["turnover"]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} settlement bar gross == −1 × R (no successor move on the transfer bar)", -R, float(b["gross"]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} successor weight after the settlement bar == 0", 0.0, float(b["weights"][1]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} next bar: nothing held, gross 0", 0.0, float(nb["gross"]), tol=args.tol)
    run_case(fx, cf)

    # 19. (Y1) marker rows on a held parent: a cash row without an amount and a spin-off without a child, both
    # status unresolved with markerReason, beside a real dividend on the next bar. The reference model books
    # nothing for them; the engine must agree bit for bit and list them as unconfirmed with the held weight.
    marker_ledger = [L("P", 3, days, "special_dividend", p_ex=100.0, p_before=100.0, marker="no_cash_amount", source="unknown"),
                     L("P", 3, days, "spin_off", p_ex=100.0, p_before=100.0, marker="no_child", source="vendor_implied"),
                     L("P", 5, days, "cash_dividend", cash=2.0, p_ex=98.0, p_before=100.0)]
    closes = {"P": [100, 100, 100, 100, 100, 98, 98, 98]}
    fx = synth_fixture("marker_rows_held", out, closes, {"P": [1] * 8}, marker_ledger)
    plain = synth_fixture("marker_rows_held_without_markers", out, closes, {"P": [1] * 8}, marker_ledger[2:])
    plain_outs = run_fixture(probe, plain, res, S, args.tol)
    def cf(mode, e):
        b = e["bars"][3]
        res.check(S, fx.name, f"D{mode} ex-bar gross with markers == 0 (nothing booked)", 0.0, float(b["gross"]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} ex-bar legsUnresolved == 2", 2, int(b["corporateActions"]["legsUnresolved"]))
        res.check(S, fx.name, f"D{mode} ex-bar distributionUnpriced == 1 (the child marker)", 1, int(b["corporateActions"]["distributionUnpriced"]))
        res.check(S, fx.name, f"D{mode} ex-bar unconfirmedCount == 2", 2, int(b["corporateActions"]["unconfirmedCount"]))
        res.check(S, fx.name, f"D{mode} ex-bar unconfirmedAbsWeight == 1 (two records of one parent count its weight once)", 1.0, float(b["corporateActions"]["unconfirmedAbsWeight"]), tol=args.tol)
        unc = e["unconfirmed"]
        res.check(S, fx.name, f"D{mode} unconfirmed records == 2 (one per marker row)", 2, len(unc))
        got = sorted(f"{u['kind']}({u['detail']})@{u['bar']} w={float(u['weightBefore']):.12g}" for u in unc)
        res.check(S, fx.name, f"D{mode} unconfirmed kinds/details/weights", "unresolved_leg(no_cash_amount)@3 w=1|unresolved_leg(no_child)@3 w=1", "|".join(got))
        res.check(S, fx.name, f"D{mode} loader legsUnbookable == 2", 2, int(e["ledgerReport"]["legsUnbookable"]))
        res.check(S, fx.name, f"D{mode} loader rowsUnresolved == 2", 2, int(e["ledgerReport"]["rowsUnresolved"]))
        res.check(S, fx.name, f"D{mode} bar 5 real dividend still booked (2/100)", 0.02, float(e["bars"][5]["corporateActions"]["dividendPnl"]), tol=args.tol)
        other = plain_outs.get(mode)
        if other and "error" not in other:
            worst = max(abs(float(a["gross"]) - float(c["gross"])) for a, c in zip(e["bars"], other["bars"]))
            res.check(S, fx.name, f"D{mode} run with markers == run without markers (max |Δgross|, bitwise)", 0.0, worst, tol=0.0)
            worst_w = max(abs(float(x or 0.0) - float(y or 0.0)) for a, c in zip(e["bars"], other["bars"]) for x, y in zip(a["weights"], c["weights"]))
            res.check(S, fx.name, f"D{mode} weights with markers == without (bitwise)", 0.0, worst_w, tol=0.0)
            res.check(S, fx.name, f"D{mode} run without markers lists nothing unconfirmed", 0, len(other["unconfirmed"]))
    run_case(fx, cf)

    # 20. (Y1) the same ledger under unresolved_policy: strict is refused on the marker bar, naming the events
    fx = synth_fixture("marker_rows_strict", out, closes, {"P": [1] * 8}, marker_ledger)
    fx.unresolved_policy = "strict"
    cfg = fx.write()
    for mode in (1, 0):
        engine = probe.run(cfg, mode, fx.dir / f"engine_D{mode}.json")
        msg = engine.get("error", "")
        res.check(S, fx.name, f"D{mode} strict refuses the run", True, "error" in engine)
        res.check(S, fx.name, f"D{mode} refusal names the policy", True, "unresolved_policy strict" in msg)
        res.check(S, fx.name, f"D{mode} refusal names the ex-date and both markers", True,
                  str(days[3].date()) in msg and "P unresolved_leg(no_cash_amount)" in msg and "P unresolved_leg(no_child)" in msg)
        res.info(S, fx.name, f"D{mode} refusal message", msg[:200])
    summary.append(fx.name)
    # ... and runs when nothing is unconfirmed
    fx = synth_fixture("no_markers_strict", out, closes, {"P": [1] * 8}, marker_ledger[2:])
    fx.unresolved_policy = "strict"
    def cf(mode, e):
        res.check(S, fx.name, f"D{mode} strict with nothing unconfirmed lists nothing", 0, len(e["unconfirmed"]))
        res.check(S, fx.name, f"D{mode} bar 5 dividend booked under strict", 0.02, float(e["bars"][5]["corporateActions"]["dividendPnl"]), tol=args.tol)
    run_case(fx, cf)

    # 21. (Y1) markers on an unheld parent list nothing (and strict does not fire)
    fx = synth_fixture("marker_rows_unheld", out, {"P": [100] * 8, "Q": [50] * 8}, {"Q": [1] * 8}, marker_ledger[:2])
    fx.unresolved_policy = "strict"
    def cf(mode, e):
        res.check(S, fx.name, f"D{mode} unheld parent: no unconfirmed records", 0, len(e["unconfirmed"]))
        res.check(S, fx.name, f"D{mode} unheld parent: legsUnresolved 0 on the ex bar", 0, int(e["bars"][3]["corporateActions"]["legsUnresolved"]))
        res.check(S, fx.name, f"D{mode} loader still schedules the markers (legsUnbookable 2)", 2, int(e["ledgerReport"]["legsUnbookable"]))
    run_case(fx, cf)

    # 22. (Y1-B) child outside the panel with a vendor-implied value 3 per parent share: (97 + 3)/100 - 1 = 0 on the ex bar,
    # distributionPnl w x 3/100, no position, record ledgerValueCash (estimate), unconfirmed estimate_value; a when-issued value
    # is a price (no unconfirmed); without the value the leg is unpriced as before (pending_dropped)
    closes = {"P": [100, 100, 100, 97, 97, 97, 97, 97]}
    for name, src, est, kinds in (("value_only_estimate", "vendor_implied", True, "estimate_value(ledger_value_cash:vendor_implied)"),
                                  ("value_only_when_issued", "wi_close", False, "")):
        fx = synth_fixture(name, out, closes, {"P": [1] * 8},
                           [L("P", 3, days, "spin_off", child="X", value=3.0, p_ex=97.0, p_before=100.0, source=src, estimate=est,
                              status="estimated" if est else "ok")])
        def cf(mode, e, kinds=kinds, name=name):
            b = e["bars"][3]
            res.check(S, name, f"D{mode} ex-bar gross == (97 + 3)/100 − 1 = 0", 0.0, float(b["gross"]), tol=args.tol)
            res.check(S, name, f"D{mode} distributionPnl == 1 × 3/100", 0.03, float(b["corporateActions"]["distributionPnl"]), tol=args.tol)
            res.check(S, name, f"D{mode} childPositionsCreated 0 / distributionUnpriced 0", "0/0",
                      f"{b['corporateActions']['childPositionsCreated']}/{b['corporateActions']['distributionUnpriced']}")
            res.check(S, name, f"D{mode} ex-bar turnover 0 (no position, no trade)", 0.0, float(b["turnover"]), tol=args.tol)
            res.check(S, name, f"D{mode} position count stays 1", 1, int(b["positionCount"]))
            recs = e["distributions"]
            res.check(S, name, f"D{mode} record source ledgerValueCash / estimate", f"ledgerValueCash/{est}",
                      f"{recs[0]['source']}/{recs[0]['estimate']}" if recs else "none")
            res.check(S, name, f"D{mode} loader legsValueOnly == 1", 1, int(e["ledgerReport"].get("legsValueOnly", -1)))
            got = "|".join(sorted(f"{u['kind']}({u['detail']})" for u in e["unconfirmed"]))
            res.check(S, name, f"D{mode} unconfirmed", kinds, got)
        run_case(fx, cf)
    fx = synth_fixture("value_only_without_value", out, closes, {"P": [1] * 8},
                       [L("P", 3, days, "spin_off", child="X", p_ex=97.0, p_before=100.0, source="vendor_implied", estimate=True, status="estimated")])
    def cf(mode, e):
        b = e["bars"][3]
        res.check(S, fx.name, f"D{mode} no value column -> unpriced, gross == −0.03", -0.03, float(b["gross"]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} distributionUnpriced 1", 1, int(b["corporateActions"]["distributionUnpriced"]))
        res.check(S, fx.name, f"D{mode} unconfirmed pending_dropped(child_not_in_panel)", "pending_dropped(child_not_in_panel)",
                  "|".join(f"{u['kind']}({u['detail']})" for u in e["unconfirmed"]))
    run_case(fx, cf)

    # 23. (user review 09-21) quote-priced spin whose ratio the ledger estimated (isEstimate, estimateReason ratio): the
    # accounting is the quote's (bit-identical to the unflagged leg), the record is an estimate and the parent is unconfirmed
    # (estimate_value(child_close:ratio)); strict refuses; the unflagged twin lists nothing
    closes = {"P": [100, 100, 100, 80, 80, 80, 80, 80], "C": [nan, nan, nan, 40, 42, 42, 42, 42]}
    flagged_rows = [L("P", 3, days, "spin_off", child="C", ratio=0.5, child_close=40.0, p_ex=80.0, p_before=100.0, estimate=True, status="estimated", reason="ratio")]
    plain_rows = [L("P", 3, days, "spin_off", child="C", ratio=0.5, child_close=40.0, p_ex=80.0, p_before=100.0)]
    plain = synth_fixture("quoted_estimate_ratio_unflagged", out, closes, {"P": [1] * 8}, plain_rows)
    plain_outs = run_fixture(probe, plain, res, S, args.tol)
    fx = synth_fixture("quoted_estimate_ratio", out, closes, {"P": [1] * 8}, flagged_rows)
    def cf(mode, e):
        b = e["bars"][3]
        res.check(S, fx.name, f"D{mode} ex-bar gross == (80 + 0.5×40)/100 − 1 = 0 (the quote prices the leg)", 0.0, float(b["gross"]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} distributionEstimated 1 on the ex bar", 1, int(b["corporateActions"]["distributionEstimated"]))
        recs = e["distributions"]
        res.check(S, fx.name, f"D{mode} record source childClose / estimate True", "childClose/True", f"{recs[0]['source']}/{recs[0]['estimate']}" if recs else "none")
        res.check(S, fx.name, f"D{mode} unconfirmed estimate_value(child_close:ratio) with the held weight", "estimate_value(child_close:ratio)@3 w=1",
                  "|".join(f"{u['kind']}({u['detail']})@{u['bar']} w={float(u['weightBefore']):.12g}" for u in e["unconfirmed"]))
        other = plain_outs.get(mode)
        if other and "error" not in other:
            worst = max(abs(float(a["gross"]) - float(c["gross"])) for a, c in zip(e["bars"], other["bars"]))
            res.check(S, fx.name, f"D{mode} flagged run == unflagged run (max |Δgross|, bitwise)", 0.0, worst, tol=0.0)
            res.check(S, fx.name, f"D{mode} unflagged twin lists nothing unconfirmed", 0, len(other["unconfirmed"]))
    run_case(fx, cf)
    fx = synth_fixture("quoted_estimate_ratio_strict", out, closes, {"P": [1] * 8}, flagged_rows)
    fx.unresolved_policy = "strict"
    cfg = fx.write()
    for mode in (1, 0):
        engine = probe.run(cfg, mode, fx.dir / f"engine_D{mode}.json")
        res.check(S, fx.name, f"D{mode} strict refuses the flagged quote-priced leg", True, "error" in engine and "P estimate_value(child_close:ratio)" in engine.get("error", ""))
    summary.append(fx.name)

    res.tables["synthetic"] = summary


# ----------------------------------------------------------------------------------------------------
# C2. Split-path multi-leg transitions (R3 rework): the cash/successor leg is an `events:` settlement of
# the old parent id, the extra child stock leg a ledger spin_off row dated on the settlement bar for
# the same parent. The coordinator's contract: cash once (events), child once (ledger), total ==
# (cash + Σ ratio × child_close)/mark − 1 on the parent exposure, no default_delisting_return.
# ----------------------------------------------------------------------------------------------------
def E(parent, end_index, days, cash=None, successor=None, ratio=None, succ_close=None, succ_index=None, mark=None,
      has_stock=None, has_cash=None):
    return dict(tradingItemId=parent, securityEndDate=days[end_index], cashPerShareCache=cash, successorTradingItemId=successor,
                successorRatioCache=ratio, hasStockLeg=(ratio is not None) if has_stock is None else has_stock,
                hasCashLeg=(cash is not None) if has_cash is None else has_cash,
                successorCacheClose=succ_close, successorCacheCloseDate=None if succ_index is None else days[succ_index],
                targetCacheMark=mark, targetCacheMarkDate=None if mark is None else days[end_index], settlementReturn=None)


def section_split_path(args, probe: Probe, res: Results, out: Path):
    S = "C2.split_path"
    nan = np.nan
    days = synth_days(8)
    table = []

    def settle_info(e, p_i, bar):
        recs = [r for r in e["settlements"] if r["asset"] == p_i]
        b = e["bars"][bar]
        return recs[0] if recs else None, b

    for w in (1.0, -1.0):
        tag = "long" if w > 0 else "short"
        # SB1: cash leg in events (14.50, no successor) + child stock leg in the ledger dated on the settlement bar
        fx = synth_fixture(f"split_cash_events_child_ledger_{tag}", out, {"P": [100, 100, 90, 100, nan, nan, nan, nan], "C": [nan, nan, nan, nan, 40, 42, 42, 42]}, {"P": [w] * 8},
                           [L("P", 4, days, "spin_off", child="C", ratio=0.5, child_close=40.0, p_ex=None, p_before=100.0)],
                           [E("P", 3, days, cash=14.5, mark=100.0)])
        outs = run_fixture(probe, fx, res, S, args.tol)
        contract = (14.5 + 0.5 * 40) / 100 - 1
        for mode in (1, 0):
            e = outs.get(mode)
            if not e or "error" in e:
                continue
            p_i, c_i = fx.idx("P"), fx.idx("C")
            rec, b = settle_info(e, p_i, 4)
            res.check(S, fx.name, f"D{mode} events priced the cash leg on the settlement bar (source computed, R = 14.5/100 − 1)", -0.855, float(rec["return"]) if rec else nan, tol=args.tol)
            res.check(S, fx.name, f"D{mode} default_delisting_return never entered (defaultDelistings 0)", 0, int(b["missingData"]["defaultDelistings"]))
            res.check(S, fx.name, f"D{mode} loader counted the settlement-bar ledger leg (legsAfterParentEnd 1)", 1, int(e["ledgerReport"]["legsAfterParentEnd"]))
            res.check(S, fx.name, f"D{mode} CONTRACT: settlement-bar gross == w × ((14.5 + 0.5×40)/100 − 1) (cash once via events + child once via the ledger)", w * contract, float(b["gross"]), tol=args.tol)
            res.check(S, fx.name, f"D{mode} CONTRACT: child weight created == w × 0.5 × 40/100 (pre-settlement exposure and mark)", w * 0.2, float(b["weights"][c_i]), tol=args.tol)
            res.check(S, fx.name, f"D{mode} diagnostics: legsOnSettlementBar 1, legsDeferredToEvents 0, one childClose record flagged onSettlementBar", "1/0/1/True",
                      f"{int(b['corporateActions']['legsOnSettlementBar'])}/{int(b['corporateActions']['legsDeferredToEvents'])}/{int(b['corporateActions']['childPositionsCreated'])}/"
                      f"{bool(e['distributions'][0]['onSettlementBar']) if e['distributions'] else None}")
            nb = e["bars"][5]
            res.check(S, fx.name, f"D{mode} documented behaviour: the child trades on the NEXT bar (trade == −received)", -w * 0.2, float(nb["trades"][c_i]), tol=args.tol)
            res.info(S, fx.name, f"D{mode} what the engine booked", f"gross {float(b['gross']):+.6f} = w × R_events ({w * float(rec['return']) if rec else nan:+.6f}) + child {float(b['corporateActions']['distributionPnl']):+.6f}; "
                     f"distributionCount {int(b['corporateActions']['distributionCount'])}, childPositionsCreated {int(b['corporateActions']['childPositionsCreated'])}, "
                     f"defaultDelistings {int(b['missingData']['defaultDelistings'])}")
            table.append(dict(case=fx.name, mode=mode, contract=w * contract, engine=float(b["gross"]), events_R=float(rec["return"]) if rec else nan,
                              ledger_booked=int(b["corporateActions"]["childPositionsCreated"]), default_used=int(b["missingData"]["defaultDelistings"]),
                              legs_after_end=int(e["ledgerReport"]["legsAfterParentEnd"]), on_bar=int(b["corporateActions"]["legsOnSettlementBar"]),
                              deferred=int(b["corporateActions"]["legsDeferredToEvents"])))

        # SB2 (DISC.A-like): successor stock leg in events (0.5 S) + two ledger children (0.5 C, 0.05 D) on the settlement bar
        fx = synth_fixture(f"split_successor_events_children_ledger_{tag}", out,
                           {"P": [18.3, 18.3, 18.3, 18.3, nan, nan, nan, nan], "S": [nan, nan, nan, nan, 13.81, 14, 14, 14],
                            "C": [nan, nan, nan, nan, 16, 16, 16, 16], "D": [nan, nan, nan, nan, 27.21, 27, 27, 27]}, {"P": [w] * 8},
                           [L("P", 4, days, "spin_off", child="C", ratio=0.5, child_close=16.0, p_ex=None, p_before=f32(18.3), seq=0),
                            L("P", 4, days, "spin_off", child="D", ratio=0.05, child_close=f32(27.21), p_ex=None, p_before=f32(18.3), seq=1)],
                           [E("P", 3, days, successor="S", ratio=0.5, succ_close=f32(13.81), succ_index=4, mark=f32(18.3))])
        outs = run_fixture(probe, fx, res, S, args.tol)
        mark = f32(18.3)
        contract = (0.5 * f32(13.81) + 0.5 * 16 + 0.05 * f32(27.21)) / mark - 1
        r_events = 0.5 * f32(13.81) / mark - 1
        for mode in (1, 0):
            e = outs.get(mode)
            if not e or "error" in e:
                continue
            p_i, s_i, c_i, d_i = fx.idx("P"), fx.idx("S"), fx.idx("C"), fx.idx("D")
            rec, b = settle_info(e, p_i, 4)
            res.check(S, fx.name, f"D{mode} events priced the successor leg (source computed, R = 0.5×13.81/18.30 − 1)", r_events, float(rec["return"]) if rec else nan, tol=args.tol)
            res.check(S, fx.name, f"D{mode} successor weight transferred == w × 0.5 × 13.81/18.30 (then rebalanced to 0 on the same bar)", w * 0.5 * f32(13.81) / mark, float(rec["transferred"]) if rec else nan, tol=args.tol)
            res.check(S, fx.name, f"D{mode} default_delisting_return never entered", 0, int(b["missingData"]["defaultDelistings"]))
            res.check(S, fx.name, f"D{mode} loader counted both settlement-bar ledger legs (legsAfterParentEnd 2)", 2, int(e["ledgerReport"]["legsAfterParentEnd"]))
            res.check(S, fx.name, f"D{mode} CONTRACT: settlement-bar gross == w × ((0.5×13.81 + 0.5×16 + 0.05×27.21)/18.30 − 1)", w * contract, float(b["gross"]), tol=args.tol)
            res.check(S, fx.name, f"D{mode} CONTRACT: child C weight == w × 0.5 × 16/18.30", w * 0.5 * 16 / mark, float(b["weights"][c_i]), tol=args.tol)
            res.check(S, fx.name, f"D{mode} CONTRACT: child D weight == w × 0.05 × 27.21/18.30", w * 0.05 * f32(27.21) / mark, float(b["weights"][d_i]), tol=args.tol)
            res.check(S, fx.name, f"D{mode} diagnostics: legsOnSettlementBar 2, legsDeferredToEvents 0, children created 2", "2/0/2",
                      f"{int(b['corporateActions']['legsOnSettlementBar'])}/{int(b['corporateActions']['legsDeferredToEvents'])}/{int(b['corporateActions']['childPositionsCreated'])}")
            nb = e["bars"][5]
            res.check(S, fx.name, f"D{mode} documented behaviour (1-bar difference): successor rebalanced on the settlement bar (trade == −transferred)", -w * 0.5 * f32(13.81) / mark, float(b["trades"][s_i]), tol=args.tol)
            res.check(S, fx.name, f"D{mode} documented behaviour (1-bar difference): ledger children trade on the NEXT bar (C trade == −received)", -w * 0.5 * 16 / mark, float(nb["trades"][c_i]), tol=args.tol)
            res.check(S, fx.name, f"D{mode} documented behaviour (1-bar difference): ledger children trade on the NEXT bar (D trade == −received)", -w * 0.05 * f32(27.21) / mark, float(nb["trades"][d_i]), tol=args.tol)
            res.check(S, fx.name, f"D{mode} next-bar turnover == |C| + |D| (children sold with the normal fee)", abs(w) * (0.5 * 16 + 0.05 * f32(27.21)) / mark, float(nb["turnover"]), tol=args.tol)
            res.info(S, fx.name, f"D{mode} what the engine booked", f"gross {float(b['gross']):+.6f} = w × R_events ({w * r_events:+.6f}) + children {float(b['corporateActions']['distributionPnl']):+.6f}, "
                     f"settlement-bar turnover {float(b['turnover']):.6f} (successor only), next-bar turnover {float(nb['turnover']):.6f} (children)")
            table.append(dict(case=fx.name, mode=mode, contract=w * contract, engine=float(b["gross"]), events_R=float(rec["return"]) if rec else nan,
                              ledger_booked=int(b["corporateActions"]["childPositionsCreated"]), default_used=int(b["missingData"]["defaultDelistings"]),
                              legs_after_end=int(e["ledgerReport"]["legsAfterParentEnd"]), on_bar=int(b["corporateActions"]["legsOnSettlementBar"]),
                              deferred=int(b["corporateActions"]["legsDeferredToEvents"])))

    # SB3: the same child leg dated on the parent's LAST trade date instead (bar 3, parent quoted, child not yet):
    # it books as pending and resolves on the settlement bar — but against the mark before the ex-date (90), not the settlement mark (100)
    fx = synth_fixture("split_child_leg_dated_last_trade_date", out, {"P": [100, 100, 90, 100, nan, nan, nan, nan], "C": [nan, nan, nan, nan, 40, 42, 42, 42]}, {"P": [1] * 8},
                       [L("P", 3, days, "spin_off", child="C", ratio=0.5, child_close=None, p_ex=100.0, p_before=90.0, source="unknown")],
                       [E("P", 3, days, cash=14.5, mark=100.0)])
    outs = run_fixture(probe, fx, res, S, args.tol)
    for mode in (1, 0):
        e = outs.get(mode)
        if not e or "error" in e:
            continue
        p_i, c_i = fx.idx("P"), fx.idx("C")
        rec, b = settle_info(e, p_i, 4)
        res.check(S, fx.name, f"D{mode} bar 3 (ex, parent quoted): leg pending, parent move 100/90 − 1 only", 100 / 90 - 1, float(e["bars"][3]["gross"]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} bar 4: events cash R = 14.5/100 − 1 plus pending child at w_ex × 0.5 × 40 / m_ex(90)", (14.5 / 100 - 1) + 0.5 * 40 / 90, float(b["gross"]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} child weight created on the settlement bar (firstQuote) == 0.5 × 40/90", 0.5 * 40 / 90, float(b["weights"][c_i]), tol=args.tol)
        res.info(S, fx.name, f"D{mode} note", f"total over bars 3-4 {float(e['bars'][3]['gross']) + float(b['gross']):+.6f} vs cash-style on the settlement mark {(14.5 + 20) / 100 - 1:+.6f}: "
                 "the child leg is valued against the pre-ex mark (90), so dating the leg on the last trade date is not equivalent")

    # SB4a: ledger cash leg dated after the parent's last trade date, with an events settlement carrying the same cash
    fx = synth_fixture("ledger_cash_after_last_trade_with_events", out, {"P": [100, 100, 100, 100, nan, nan, nan, nan]}, {"P": [1] * 8},
                       [L("P", 4, days, "cash_dividend", cash=14.5, p_ex=None, p_before=100.0)],
                       [E("P", 3, days, cash=14.5, mark=100.0)])
    outs = run_fixture(probe, fx, res, S, args.tol)
    for mode in (1, 0):
        e = outs.get(mode)
        if not e or "error" in e:
            continue
        p_i = fx.idx("P")
        rec, b = settle_info(e, p_i, 4)
        res.check(S, fx.name, f"D{mode} loader counts the post-end ledger cash leg (legsAfterParentEnd 1)", 1, int(e["ledgerReport"]["legsAfterParentEnd"]))
        res.check(S, fx.name, f"D{mode} documented behaviour: events row carries the cash → ledger cash leg deferred (legsOnSettlementBar 1, legsDeferredToEvents 1)", "1/1",
                  f"{int(b['corporateActions']['legsOnSettlementBar'])}/{int(b['corporateActions']['legsDeferredToEvents'])}")
        res.check(S, fx.name, f"D{mode} engine books the cash once via events (R = 14.5/100 − 1)", 14.5 / 100 - 1, float(b["gross"]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} ledger cash leg not booked (dividendCount 0, dividendPnl 0)", 0.0, float(b["corporateActions"]["dividendPnl"]) + int(b["corporateActions"]["dividendCount"]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} no double count: settlement P&L == gross", float(b["missingData"]["delistingPnl"]), float(b["gross"]), tol=args.tol)

    # SB4c: the events row has NO cash leg (successor only) and the ledger carries the cash on the settlement bar:
    # the ledger cash leg is booked once against the pre-settlement mark beside the events transfer
    fx = synth_fixture("ledger_cash_on_settlement_bar_events_without_cash", out, {"P": [100, 100, 100, 100, nan, nan, nan, nan], "S": [50, 50, 50, 50, 52, 52, 52, 52]}, {"P": [1] * 8},
                       [L("P", 4, days, "cash_dividend", cash=14.5, p_ex=None, p_before=100.0)],
                       [E("P", 3, days, successor="S", ratio=1.0, succ_close=52.0, succ_index=4, mark=100.0)])
    outs = run_fixture(probe, fx, res, S, args.tol)
    for mode in (1, 0):
        e = outs.get(mode)
        if not e or "error" in e:
            continue
        p_i = fx.idx("P")
        rec, b = settle_info(e, p_i, 4)
        total = (14.5 + 1.0 * 52) / 100 - 1
        res.check(S, fx.name, f"D{mode} events R == 52/100 − 1 (successor only)", 52 / 100 - 1, float(rec["return"]) if rec else nan, tol=args.tol)
        res.check(S, fx.name, f"D{mode} documented behaviour: no events cash → ledger cash leg booked against the pre-settlement mark (dividendPnl 0.145)", 0.145, float(b["corporateActions"]["dividendPnl"]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} CONTRACT: settlement-bar gross == (14.5 + 1.0×52)/100 − 1", total, float(b["gross"]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} legsOnSettlementBar 1, legsDeferredToEvents 0", "1/0", f"{int(b['corporateActions']['legsOnSettlementBar'])}/{int(b['corporateActions']['legsDeferredToEvents'])}")

    # SB6: a ledger stock leg naming the events successor itself on the settlement bar is delivered by the transfer only
    fx = synth_fixture("ledger_stock_leg_names_the_successor", out, {"P": [100, 100, 100, 100, nan, nan, nan, nan], "S": [50, 50, 50, 50, 52, 52, 52, 52]}, {"P": [-1] * 8},
                       [L("P", 4, days, "spin_off", child="S", ratio=1.0, child_close=52.0, p_ex=None, p_before=100.0)],
                       [E("P", 3, days, successor="S", ratio=1.0, succ_close=52.0, succ_index=4, mark=100.0)])
    outs = run_fixture(probe, fx, res, S, args.tol)
    for mode in (1, 0):
        e = outs.get(mode)
        if not e or "error" in e:
            continue
        p_i, s_i = fx.idx("P"), fx.idx("S")
        rec, b = settle_info(e, p_i, 4)
        res.check(S, fx.name, f"D{mode} documented behaviour: successor named by the ledger leg → deferred to the transfer (legsDeferredToEvents 1, children 0)", "1/1/0",
                  f"{int(b['corporateActions']['legsOnSettlementBar'])}/{int(b['corporateActions']['legsDeferredToEvents'])}/{int(b['corporateActions']['childPositionsCreated'])}")
        res.check(S, fx.name, f"D{mode} delivered once: transferred == −1 × 52/100, settlement-bar gross == −1 × (52/100 − 1)", f"{-0.52:.12g}/{0.48:.12g}",
                  f"{float(rec['transferred']) if rec else nan:.12g}/{float(b['gross']):.12g}")
        res.check(S, fx.name, f"D{mode} successor weight after the bar == 0 (rebalanced), no double delivery", 0.0, float(b["weights"][s_i]), tol=args.tol)

    # SB4b: the same ledger cash leg without any events row: the parent is merely frozen, so the leg IS booked against the last mark
    fx = synth_fixture("ledger_cash_after_last_quote_no_events", out, {"P": [100, 100, 100, 100, nan, nan, nan, nan]}, {"P": [1] * 8},
                       [L("P", 4, days, "cash_dividend", cash=14.5, p_ex=None, p_before=100.0)])
    outs = run_fixture(probe, fx, res, S, args.tol)
    for mode in (1, 0):
        e = outs.get(mode)
        if not e or "error" in e:
            continue
        b = e["bars"][4]
        res.check(S, fx.name, f"D{mode} without an events row the frozen holding receives the cash leg (dividendPnl 0.145)", 0.145, float(b["corporateActions"]["dividendPnl"]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} holding stays frozen (frozenGross 1) — the export must drop such legs for ended securities", 1.0, float(b["missingData"]["frozenGross"]), tol=args.tol)

    # SB5 (TRXX-like): events row with cash 2.50 + 0.2 successor whose cache units could not be built (successorRatioCache/cashPerShareCache
    # blank, hasStockLeg/hasCashLeg true, settlementReturn blank): the declared stock leg cannot be priced → default_delisting_return
    fx = synth_fixture("events_blank_terms_default_return", out, {"P": [100, 100, 100, 100, nan, nan, nan, nan], "S": [nan] * 8}, {"P": [-1] * 8},
                       events=[E("P", 3, days, successor="S", has_stock=True, has_cash=True)])
    outs = run_fixture(probe, fx, res, S, args.tol)
    for mode in (1, 0):
        e = outs.get(mode)
        if not e or "error" in e:
            continue
        p_i = fx.idx("P")
        rec, b = settle_info(e, p_i, 4)
        res.check(S, fx.name, f"D{mode} settlement source == default (declared legs without cache-unit values)", "default", rec["source"] if rec else "none")
        res.check(S, fx.name, f"D{mode} default_delisting_return 0 entered (defaultDelistings 1, gross 0)", 0.0, float(b["gross"]), tol=args.tol)
        res.check(S, fx.name, f"D{mode} defaultDelistings == 1", 1, int(b["missingData"]["defaultDelistings"]))
        res.info(S, fx.name, f"D{mode} note", "cash 2.50 + 0.2 successor are not booked at all: a blank settlementReturn on a row whose legs cannot be priced in cache units settles at the 0 assumption")
    res.tables["split_path"] = table


# ----------------------------------------------------------------------------------------------------
# C3. Real split-path case: SLM 2014-05-01 (old SLM 2644409 → NAVI 262300822 as the events successor 1:1;
# the retained new SLM 263819154 as a ledger spin_off row keyed to the OLD id on the settlement date,
# ratio 1 per old share; R3 for_R1_events.csv rows 20/21). Two keyings are run so the decision is
# documented by a test: (a) ledger row keyed to the old id (R3's design), (b) keyed to the successor
# on its first bar (the failure mode being guarded against).
# ----------------------------------------------------------------------------------------------------
def section_real_slm(args, cache: Cache, probe: Probe, res: Results, out: Path):
    S = "C3.real_split"
    old_slm, navi, new_slm = 2644409, 262300822, 263819154
    end, settle = pd.Timestamp("2014-04-30"), pd.Timestamp("2014-05-01")
    ids = sorted([old_slm, navi, new_slm])
    cal = cache.calendar
    i_s = int(np.flatnonzero(cal == settle)[0])
    days = cal[i_s - 7: i_s + 4]
    raw = cache.raw_closes(ids, days[0], days[-1])
    adj = cache.adj(raw)
    symbols = sorted(str(i) for i in ids)
    labels = {str(old_slm): "SLM(old)", str(navi): "NAVI", str(new_slm): "SLM(new)"}
    order = [int(x) for x in symbols]
    close = adj[order].to_numpy(np.float32)
    mark = f32(adj.at[end, old_slm])
    navi_close = f32(adj.at[settle, navi])
    slm_close = f32(adj.at[settle, new_slm])
    for what, mine, theirs in (("old SLM last close 2014-04-30", mark, 25.75), ("NAVI 2014-05-01", navi_close, 17.00), ("new SLM 2014-05-01", slm_close, 9.02)):
        res.check(S, "SLM_2014", f"cache raw close ({what})", theirs, mine, tol=1e-5, kind="rel")
    res.check(S, "SLM_2014", "split factors 1.0 on both dates (cache units == raw)", 1.0, cache.sf(old_slm, end) * cache.sf(navi, settle) * cache.sf(new_slm, settle), tol=0.0)
    res.check(S, "SLM_2014", "old SLM has no quote on the settlement date", True, bool(np.isnan(adj.at[settle, old_slm])))
    contract = (1.0 * navi_close + 1.0 * slm_close) / mark - 1.0
    r_events = 1.0 * navi_close / mark - 1.0
    table = []
    for w in (1.0, -1.0):
        tag = "long" if w > 0 else "short"
        for keying in ("old_id", "successor_id"):
            T, N = close.shape
            sig = np.full((T, N), np.nan, dtype=np.float32)
            sig[:, symbols.index(str(old_slm))] = w
            name = f"SLM_2014_{tag}_ledger_keyed_{keying}"
            fx = Fixture(name=name, dir=out / "real" / name, symbols=symbols, dates=days, close=close, sig=sig, labels=labels)
            fx.event_rows = [dict(tradingItemId=str(old_slm), securityEndDate=end, cashPerShareCache=None, successorTradingItemId=str(navi),
                                  successorRatioCache=1.0, hasStockLeg=True, hasCashLeg=False, successorCacheClose=navi_close, successorCacheCloseDate=settle,
                                  targetCacheMark=mark, targetCacheMarkDate=end, settlementReturn=None)]
            if keying == "old_id":
                fx.ledger_rows = [dict(tradingItemId=str(old_slm), exDate=settle, eventType="spin_off", legType="stock", cashPerShareCache=None,
                                       childTradingItemId=str(new_slm), childRatioCache=1.0, childCloseAtExCache=slm_close, parentCloseAtExCache=None,
                                       parentCloseBeforeCache=mark, parentFactorCache=1.0, payDate=settle, isEstimate=True, status="estimated",
                                       childValueSource="child_close_ex", eventId=f"{old_slm}_2014-05-01_spin_off_1")]
            else:
                fx.ledger_rows = [dict(tradingItemId=str(navi), exDate=settle, eventType="spin_off", legType="stock", cashPerShareCache=None,
                                       childTradingItemId=str(new_slm), childRatioCache=1.0, childCloseAtExCache=slm_close, parentCloseAtExCache=navi_close,
                                       parentCloseBeforeCache=f32(adj.at[end, navi]), parentFactorCache=1.0, payDate=settle, isEstimate=True, status="estimated",
                                       childValueSource="child_close_ex", eventId=f"{navi}_2014-05-01_spin_off_1")]
            outs = run_fixture(probe, fx, res, S, args.tol)
            p_i, n_i, c_i = fx.idx(str(old_slm)), fx.idx(str(navi)), fx.idx(str(new_slm))
            bar = fx.bar(settle)
            for mode in (1, 0):
                e = outs.get(mode)
                if not e or "error" in e:
                    continue
                b = e["bars"][bar]
                recs = [r for r in e["settlements"] if r["asset"] == p_i]
                res.check(S, name, f"D{mode} old SLM settles once via events (one record, source computed)", "1/computed", f"{len(recs)}/{recs[0]['source'] if recs else 'none'}")
                if recs:
                    res.check(S, name, f"D{mode} events R == 17.00/25.75 − 1 (NAVI 1:1, no cash)", r_events, float(recs[0]["return"]), tol=args.tol)
                    res.check(S, name, f"D{mode} transferred to NAVI == w × 17.00/25.75", w * navi_close / mark, float(recs[0]["transferred"]), tol=args.tol)
                res.check(S, name, f"D{mode} default_delisting_return never entered", 0, int(b["missingData"]["defaultDelistings"]))
                res.check(S, name, f"D{mode} NAVI rebalanced to the strategy target 0 on the settlement bar (turnover == |transferred|)", abs(w * navi_close / mark), float(b["turnover"]), tol=args.tol)
                created = float(b["corporateActions"]["distributionPnl"])
                if keying == "old_id":
                    res.check(S, name, f"D{mode} loader counted the settlement-bar ledger leg (legsAfterParentEnd 1)", 1, int(e["ledgerReport"]["legsAfterParentEnd"]))
                    res.check(S, name, f"D{mode} CONTRACT: settlement-bar gross == w × ((17.00 + 9.02)/25.75 − 1)", w * contract, float(b["gross"]), tol=args.tol)
                    res.check(S, name, f"D{mode} CONTRACT: new SLM child weight == w × 9.02/25.75 from the parent's pre-settlement exposure", w * slm_close / mark, float(b["weights"][c_i]), tol=args.tol)
                    res.check(S, name, f"D{mode} diagnostics: legsOnSettlementBar 1, legsDeferredToEvents 0, record onSettlementBar", "1/0/True",
                              f"{int(b['corporateActions']['legsOnSettlementBar'])}/{int(b['corporateActions']['legsDeferredToEvents'])}/{bool(e['distributions'][0]['onSettlementBar']) if e['distributions'] else None}")
                    nb = e["bars"][bar + 1]
                    res.check(S, name, f"D{mode} documented behaviour: NAVI rebalanced on the settlement bar, new SLM sold the next bar (trade == −received)", -w * slm_close / mark, float(nb["trades"][c_i]), tol=args.tol)
                else:
                    res.check(S, name, f"D{mode} keyed to the successor: engine books nothing (NAVI carried weight 0 at booking; the transfer lands after)", 0.0, created, tol=0.0)
                    res.check(S, name, f"D{mode} keyed to the successor: settlement-bar gross == w × R_events only", w * r_events, float(b["gross"]), tol=args.tol)
                    res.info(S, name, f"D{mode} note", f"even with the transfer applied first the leg would be valued as (w × 17/25.75) × 9.02 / 25.75 = {w * navi_close / mark * slm_close / mark:+.6f}, "
                             f"not w × 9.02/25.75 = {w * slm_close / mark:+.6f}: keying to the successor is wrong in any ordering")
                res.info(S, name, f"D{mode} what the engine booked", f"gross {float(b['gross']):+.6f}, events R {float(recs[0]['return']) if recs else nan:+.6f}, "
                         f"ledger children created {int(b['corporateActions']['childPositionsCreated'])} (value {created:+.6f}), contract total {w * contract:+.6f}, "
                         f"difference {w * contract - float(b['gross']):+.6f}")
                table.append(dict(case=name, mode=mode, contract=w * contract, engine=float(b["gross"]), events_R=float(recs[0]["return"]) if recs else math.nan,
                                  ledger_booked=int(b["corporateActions"]["childPositionsCreated"]), default_used=int(b["missingData"]["defaultDelistings"]),
                                  legs_after_end=int(e["ledgerReport"]["legsAfterParentEnd"]), on_bar=int(b["corporateActions"]["legsOnSettlementBar"]),
                                  deferred=int(b["corporateActions"]["legsDeferredToEvents"])))
    # (c) the stitched successor line is what the strategy holds before the transition (NAVI carries the old
    # closes in the cache), the leg keyed to it: the ledger path alone books the retained SLM correctly.
    for w in (1.0, -1.0):
        tag = "long" if w > 0 else "short"
        T, N = close.shape
        sig = np.full((T, N), np.nan, dtype=np.float32)
        sig[:, symbols.index(str(navi))] = w
        name = f"SLM_2014_{tag}_stitched_line_held_keyed_successor_id"
        fx = Fixture(name=name, dir=out / "real" / name, symbols=symbols, dates=days, close=close, sig=sig, labels=labels)
        fx.ledger_rows = [dict(tradingItemId=str(navi), exDate=settle, eventType="spin_off", legType="stock", cashPerShareCache=None,
                               childTradingItemId=str(new_slm), childRatioCache=1.0, childCloseAtExCache=slm_close, parentCloseAtExCache=navi_close,
                               parentCloseBeforeCache=f32(adj.at[end, navi]), parentFactorCache=1.0, payDate=settle, isEstimate=True, status="estimated",
                               childValueSource="child_close_ex", eventId=f"{navi}_2014-05-01_spin_off_1")]
        outs = run_fixture(probe, fx, res, S, args.tol)
        n_i, c_i = fx.idx(str(navi)), fx.idx(str(new_slm))
        bar = fx.bar(settle)
        for mode in (1, 0):
            e = outs.get(mode)
            if not e or "error" in e:
                continue
            b = e["bars"][bar]
            res.check(S, name, f"D{mode} stitched line held: gross == w × ((17.00 + 9.02)/25.75 − 1) (price move −33.98% + ledger child +35.03%)", w * contract, float(b["gross"]), tol=args.tol)
            res.check(S, name, f"D{mode} new SLM child weight == w × 9.02/25.75", w * slm_close / mark, float(b["weights"][c_i]), tol=args.tol)
            res.check(S, name, f"D{mode} NAVI weight unchanged (no settlement, no transfer)", w, float(b["weights"][n_i]), tol=args.tol)
            table.append(dict(case=name, mode=mode, contract=w * contract, engine=float(b["gross"]), events_R=math.nan,
                              ledger_booked=int(b["corporateActions"]["childPositionsCreated"]), default_used=0, legs_after_end=int(e["ledgerReport"]["legsAfterParentEnd"]),
                              on_bar=int(b["corporateActions"]["legsOnSettlementBar"]), deferred=int(b["corporateActions"]["legsDeferredToEvents"])))
    # what the R1 export currently carries for this transition (informational)
    path = Path(args.ledger)
    if path.exists():
        L = pq.read_table(path, filters=[("tradingItemId", "in", [str(old_slm), str(navi)])]).to_pandas()
        rows = L[(L.exDate == settle)]
        for _, r in rows.iterrows():
            res.info(S, "SLM_2014", f"R1 export row keyed to {labels.get(str(r.tradingItemId), r.tradingItemId)}",
                     f"{r.eventType}/{r.legType} child {r.childTradingItemId} ratioCache {r.childRatioCache} childClose {r.childCloseAtExCache} "
                     f"parentBefore {r.parentCloseBeforeCache} status {r.status} source {r.childValueSource}",
                     note="old-id row with a finite ratioCache books on the settlement bar (E1 §9); ratio NaN → 'unpriced'; a successor-keyed row books only for a strategy holding the successor line")
    res.tables.setdefault("split_path", []).extend(table)


# ----------------------------------------------------------------------------------------------------
# D. Double counting: engine-with-ledger ex-date P&L vs w × returns_total v2.
# ----------------------------------------------------------------------------------------------------
def section_double_count(args, res: Results, real_rows: list[dict]):
    S = "D.double_count"
    path = Path(args.returns_v2)
    if not path.exists():
        res.info(S, "-", "returns_total v2 not found; section skipped", str(path))
        return
    ids = sorted({int(r["units"]["parent"]) for r in real_rows})
    dates = sorted({pd.Timestamp(r["ex"]) for r in real_rows})
    t = pq.read_table(path, filters=[("tradingItemId", "in", ids), ("date", ">=", min(dates)), ("date", "<=", max(dates))]).to_pandas()
    table = []
    for r in real_rows:
        parent = int(r["units"]["parent"])
        ex = pd.Timestamp(r["ex"])
        hit = t[(t.tradingItemId == parent) & (t.date == ex)]
        if len(hit) != 1:
            res.check(S, r["case"], "returns_total v2 row for the parent ex-date", 1, len(hit))
            continue
        ret_v2 = float(hit.ret.iloc[0])
        flag = int(hit.retFlag.iloc[0])
        u = r["units"]
        # cash-style formula in raw units (float64 on the float32 raw closes) — R2's definition
        pf = u["sf_parent_prev"] / u["sf_parent_ex"]          # new parent shares per old share (HON 2026-06-29: 0.5)
        raw_formula = (pf * u["raw_ex"] + float(u["cash_cache"]) * u["sf_parent_prev"] + (u["ratio_cache"] * u["sf_parent_prev"] / u["sf_child_ex"]) * u["raw_child"]) / u["raw_prev"] - 1.0
        res.check(S, r["case"], "returns_total v2 == cash-style formula on raw closes", raw_formula, ret_v2, tol=1e-9)
        res.check(S, r["case"], "retFlag == 2 (distribution)", 2, flag)
        row = dict(case=r["case"], ret_v2=ret_v2, retFlag=flag, w=r["w"], no_ledger_pnl=r["w"] * ret_v2)
        for mode in (1, 0):
            key = f"engine_D{mode}"
            if key not in r:
                continue
            w = r[f"w_carried_D{mode}"]              # the weight the engine actually carried into the ex-date
            engine_pnl = r[key]
            row[key] = engine_pnl
            row[f"diff_D{mode}"] = engine_pnl - w * ret_v2
            row[f"ledger_component_D{mode}"] = r.get(f"child_w_D{mode}", math.nan)
            # identity up to the float32 restatement of the split-adjusted panel (exact when sf == 1 or a power of two)
            tol = args.tol if u["sf_parent_prev"] in (1.0, 0.5, 2.0) and u["sf_child_ex"] in (1.0, 0.5, 2.0) else 1e-6
            res.check(S, r["case"], f"D{mode} engine-with-ledger ex-date P&L == w × returns_total v2 (tol {tol:g}; f32 restatement when sf is not a power of two)",
                      w * ret_v2, engine_pnl, tol=tol, note=f"sf_parent_prev={u['sf_parent_prev']}")
            # what a returns_total-based engine that ALSO booked the ledger would report: double the distribution
            double = w * ret_v2 + r.get(f"child_w_D{mode}", 0.0)
            res.check(S, r["case"], f"D{mode} adding the ledger on top of returns_total would double count the child value (|excess| == |child weight|)",
                      abs(r.get(f"child_w_D{mode}", 0.0)), abs(double - engine_pnl), tol=1e-6)
        table.append(row)
    res.tables["double_count"] = table


# ----------------------------------------------------------------------------------------------------
# E. Real-data export identity (R1 engine/ca_ledger_cache.parquet).
# ----------------------------------------------------------------------------------------------------
def section_export_identity(args, cache: Cache, probe: Probe, res: Results, real_rows: list[dict], out: Path):
    S = "E.export"
    path = Path(args.ledger)
    if not path.exists():
        res.info(S, "-", "R1 export not found; section skipped", str(path))
        return
    res.info(S, "-", "R1 export file", f"{path} sha256 {sha256(path)[:16]}… ({path.stat().st_size} bytes, {time.strftime('%Y-%m-%d %H:%M', time.localtime(path.stat().st_mtime))})")
    L = pq.read_table(path).to_pandas()
    # the engine refuses the whole file on a repeated (parent, exDate, legType, child) key (§3-1 uniqueness): pre-check it here
    keyed = L[~L.eventType.isin(["merger_cash", "merger_stock", "rename", "relist"])]
    key = keyed[["tradingItemId", "exDate", "legType", "childTradingItemId"]].astype(str)
    dups = keyed[key.duplicated(keep=False)]
    res.check(S, "-", "R1 export has no repeated (parent, exDate, legType, child) key", 0, len(dups),
              note="; ".join(f"{r.tradingItemId} {pd.Timestamp(r.exDate).date()} {r.eventType}/{r.legType} child {r.childTradingItemId}" for _, r in dups.head(6).iterrows()))
    res.info(S, "-", "R1 export rows / legTypes", f"{len(L)} rows; {L.legType.value_counts().to_dict()}; status {L.status.value_counts().to_dict()}")
    if "markerReason" in L.columns:
        # Y1 marker rows: status unresolved, values NaN, child null, reason matching the gap
        M = L[L.markerReason.notna()]
        res.info(S, "-", "R1 export marker rows (Y1)", f"{len(M)} rows; {M.markerReason.value_counts().to_dict()}")
        nan_cash = (L.legType == "cash") & ~np.isfinite(L.cashPerShareCache)
        no_child = L.legType.isin(["stock", "rights"]) & L.childTradingItemId.isna()
        res.check(S, "-", "marker rows are status unresolved / isEstimate False / values NaN", 0,
                  int(((M.status != "unresolved") | M.isEstimate.astype(bool) | np.isfinite(M.cashPerShareCache) | np.isfinite(M.childRatioCache) | np.isfinite(M.childCloseAtExCache)).sum()))
        # Y1-B: a no-child row with a usable per-share value is a value leg, not a marker
        valued = np.zeros(len(L), dtype=bool)
        if "childValuePerShareCache" in L.columns:
            _v = L.childValuePerShareCache.to_numpy(np.float64)
            valued = (np.isfinite(_v) & (_v > 0) & (L.status != "unresolved").to_numpy()
                      & L.childValueSource.isin(["wi_close", "event_value", "vendor_implied"]).to_numpy())
        res.check(S, "-", "every cash row without an amount / value row without a child is a marker row (or a valued leg)", 0,
                  int(((nan_cash | (no_child & ~valued)) & L.markerReason.isna()).sum()))
        res.info(S, "-", "R1 export value legs without a child id (Y1-B, booked as ledgerValueCash)", int((no_child & valued).sum()))
    if "estimateReason" in L.columns:
        flagged = L[(L.isEstimate.astype(bool) | (L.status == "estimated")) & L.markerReason.isna()]
        res.check(S, "-", "every flagged export row carries an estimateReason", 0, int(flagged.estimateReason.isna().sum()))
        res.check(S, "-", "unflagged export rows carry no estimateReason", 0, int(L[~(L.isEstimate.astype(bool) | (L.status == "estimated"))].estimateReason.notna().sum()))
        quoted = flagged[flagged.legType.isin(["stock", "rights"]) & (flagged.childValueSource == "child_close_ex") & np.isfinite(flagged.childCloseAtExCache)]
        res.info(S, "-", "R1 export quote-priced stock/rights rows flagged as estimates (ratio/date estimated)",
                 f"{len(quoted)} rows; reasons {quoted.estimateReason.value_counts().to_dict()}; parents {sorted(quoted.tradingItemId.unique().tolist())}")
        section_quoted_estimates(args, res, quoted, path, out, probe)
        res.check(S, "-", "markerReason matches the gap (no_cash_amount ↔ NaN cash, no_child ↔ null child)", 0,
                  int(((L.markerReason == "no_cash_amount") & ~nan_cash).sum() + ((L.markerReason == "no_child") & ~no_child).sum()))
    else:
        res.info(S, "-", "R1 export has no markerReason column (pre-Y1 export)", "")
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from loader import CacheV21  # the reference loader whose split_adj basis the export targets
        loader_ok = True
    except Exception as exc:  # pragma: no cover
        loader_ok = False
        res.info(S, "-", "reference loader import failed; split_adj cross-check skipped", str(exc))
    table = []
    for r in real_rows:
        if r["case"].endswith("_book"):
            continue
        u = r["units"]
        parent, child, ex = str(int(u["parent"])), str(int(u["child"])), pd.Timestamp(r["ex"])
        rows = L[(L.tradingItemId == parent) & (L.exDate == ex)]
        stock = rows[rows.legType == "stock"]
        res.check(S, r["case"], "R1 export has exactly one stock row for (parent, exDate)", 1, len(stock))
        if len(stock) != 1:
            continue
        s = stock.iloc[0]
        res.check(S, r["case"], "R1 childTradingItemId", child, str(s.childTradingItemId))
        res.check(S, r["case"], "R1 childRatioCache == childRatio × sf_child(ex) / sf_parent(prev)", u["ratio_cache"], float(s.childRatioCache), tol=1e-12)
        res.check(S, r["case"], "R1 parentCloseBeforeCache == f32(raw_prev / sf_parent(prev))", u["p_before"], float(s.parentCloseBeforeCache), tol=1e-9, kind="rel")
        res.check(S, r["case"], "R1 parentCloseAtExCache == f32(raw_ex / sf_parent(ex))", u["p_ex"], float(s.parentCloseAtExCache), tol=1e-9, kind="rel")
        res.check(S, r["case"], "R1 childCloseAtExCache == f32(raw_child / sf_child(ex))", u["c_ex"], float(s.childCloseAtExCache), tol=1e-9, kind="rel")
        res.check(S, r["case"], "R1 parentFactorCache == 1", 1.0, float(s.parentFactorCache), tol=0.0)
        res.check(S, r["case"], "R1 childValueSource child_close_ex / status ok / isEstimate False", "child_close_ex/ok/False", f"{s.childValueSource}/{s.status}/{bool(s.isEstimate)}")
        # cache units ≠ raw when the parent has a later split (DD) — and the P&L identity still holds
        ident_cache = (float(s.parentCloseAtExCache) + float(s.childRatioCache) * float(s.childCloseAtExCache)) / float(s.parentCloseBeforeCache) - 1.0
        ident_raw = (u["raw_ex"] * (u["sf_parent_prev"] / u["sf_parent_ex"]) + float(s.childRatioCache) * u["sf_parent_prev"] / u["sf_child_ex"] * u["raw_child"]) / u["raw_prev"] - 1.0
        res.check(S, r["case"], "cash-style return from R1 cache units == raw-unit formula (f32 restatement)", ident_raw, ident_cache, tol=1e-6)
        row = dict(case=r["case"], ratio_cache_R1=float(s.childRatioCache), ratio_raw=float(s.childRatioCache) * u["sf_parent_prev"] / u["sf_child_ex"],
                   before_R1=float(s.parentCloseBeforeCache), raw_prev=u["raw_prev"], sf_parent_prev=u["sf_parent_prev"], ex_R1=float(s.parentCloseAtExCache),
                   child_R1=float(s.childCloseAtExCache), ret_cache=ident_cache, ret_raw=ident_raw)
        if loader_ok:
            try:
                cv = CacheV21(cache.root, master_root=cache.root, ids=[int(parent), int(child)],
                              start=(ex - pd.Timedelta(days=20)).strftime("%Y-%m-%d"), end=(ex + pd.Timedelta(days=10)).strftime("%Y-%m-%d"))
                panel = cv.get_panel("close", "split_adj")
                res.check(S, r["case"], "loader split_adj parent close before ex == R1 parentCloseBeforeCache (bitwise)",
                          float(panel.at[u["prev_panel"], int(parent)]), float(s.parentCloseBeforeCache), tol=0.0)
                res.check(S, r["case"], "loader split_adj parent close at ex == R1 parentCloseAtExCache (bitwise)",
                          float(panel.at[ex, int(parent)]), float(s.parentCloseAtExCache), tol=0.0)
                res.check(S, r["case"], "loader split_adj child close at ex == R1 childCloseAtExCache (bitwise)",
                          float(panel.at[ex, int(child)]), float(s.childCloseAtExCache), tol=0.0)
                fxc = r["fixture"].close
                fx = r["fixture"]
                mine = fxc[fx.bar(ex), fx.idx(parent)]
                res.check(S, r["case"], "fixture split_adj close == loader split_adj close (bitwise)", float(panel.at[ex, int(parent)]), float(mine), tol=0.0)
            except Exception as exc:
                res.check(S, r["case"], "reference loader split_adj cross-check", "ok", f"error: {exc}")
        # the engine run with the R1 file as the ledger must reproduce the own-ledger run bit for bit
        fx = r["fixture"]
        fx2 = Fixture(name=fx.name + "_R1ledger", dir=out / "real" / (fx.name + "_R1ledger"), symbols=fx.symbols, dates=fx.dates, close=fx.close,
                      sig=fx.sig, ledger_rows=fx.ledger_rows, warmup=fx.warmup, labels=fx.labels, ledger_path_override=path)
        cfg = fx2.write()
        for mode in (1, 0):
            engine = probe.run(cfg, mode, fx2.dir / f"engine_D{mode}.json")
            if "error" in engine:
                res.check(S, r["case"], f"D{mode} engine accepts the R1 export as the ledger (staleness guard vs the fixture panel)", "ok", f"error: {engine['error']}")
                continue
            own = json.loads((fx.dir / f"engine_D{mode}.json").read_text())
            worst = max(abs(float(a["gross"]) - float(b["gross"])) for a, b in zip(engine["bars"], own["bars"]))
            res.check(S, r["case"], f"D{mode} R1-ledger run == own-ledger run (max |Δgross|, bitwise)", 0.0, worst, tol=0.0)
            res.check(S, r["case"], f"D{mode} R1 rows matched for the parent inside the panel dates",
                      f"legs={own['ledgerReport']['legs']}", f"legs={engine['ledgerReport']['legs']}")
            row[f"R1_legs_D{mode}"] = engine["ledgerReport"]["legs"]
            row[f"R1_rowsQuantity_D{mode}"] = engine["ledgerReport"]["rowsQuantity"]
            row[f"R1_priceChecks_D{mode}"] = engine["ledgerReport"]["priceChecks"]
        table.append(row)
    res.tables["export"] = table


def section_quoted_estimates(args, res: Results, quoted: pd.DataFrame, ledger_path: Path, out: Path, probe: Probe):
    """(user review 09-21) Every quote-priced stock/rights row the export flags as an estimate must surface as an
    unconfirmed estimate_value(child_close:<reason>) when its parent is held: one fixture per parent built from the
    cache root (parent + the row's children, split_adj lifecycle-cut closes around the ex-date), the full export as
    the ledger, the parent held long from the first bar."""
    S = "E.quoted_estimates"
    if quoted.empty:
        res.info(S, "-", "no quote-priced flagged rows in the export", "")
        return
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        from loader import CacheV21
    except Exception as exc:  # pragma: no cover
        res.info(S, "-", "reference loader import failed; section skipped", str(exc))
        return
    root = Path(args.cache_root)
    table = []
    for (parent, ex), rows in quoted.groupby(["tradingItemId", "exDate"]):
        label = f"{parent}_{pd.Timestamp(ex).date()}"
        children = [int(c) for c in rows.childTradingItemId.dropna().astype(str).tolist()]
        try:
            cv = CacheV21(root, master_root=root, ids=[int(parent)] + children, start=(pd.Timestamp(ex) - pd.Timedelta(days=12)).strftime("%Y-%m-%d"),
                          end=(pd.Timestamp(ex) + pd.Timedelta(days=8)).strftime("%Y-%m-%d"))
            adj = cv.get_panel("close", "split_adj", cut="lifecycle")
        except Exception as exc:
            res.check(S, label, "fixture panel from the cache root", "ok", f"error: {exc}")
            continue
        symbols = sorted(str(c) for c in adj.columns)
        days = adj.index
        close = np.array([adj[int(s_)].to_numpy(np.float64) for s_ in symbols], dtype=np.float32).T
        sig = np.full((len(days), len(symbols)), np.nan, dtype=np.float32)
        sig[:, symbols.index(str(parent))] = 1.0
        fx = Fixture(name=f"quoted_estimate_{label}", dir=out / "quoted_estimates" / label, symbols=symbols, dates=days, close=close, sig=sig,
                     warmup=1, labels={s_: s_ for s_ in symbols}, ledger_path_override=ledger_path)
        cfg = fx.write()
        engine = probe.run(cfg, 1, fx.dir / "engine_D1.json")
        if "error" in engine:
            res.check(S, label, "D1 engine run with the full export as the ledger", "ok", f"error: {engine['error'][:200]}")
            continue
        got = {(engine["symbols"][u["asset"]], u["kind"], u["detail"]) for u in engine["unconfirmed"] if u["bar"] == fx.bar(ex)}
        for _, r in rows.iterrows():
            want = (str(parent), "estimate_value", f"child_close:{r.estimateReason}")
            res.check(S, label, f"child {r.childTradingItemId} listed as {want[1]}({want[2]}) on the ex bar", True, want in got,
                      note="; ".join(f"{a} {k}({d})" for a, k, d in sorted(got)))
        est = int(engine["bars"][fx.bar(ex)]["corporateActions"]["distributionEstimated"])
        res.check(S, label, "distributionEstimated on the ex bar >= flagged quoted rows", True, est >= len(rows), note=f"distributionEstimated {est}")
        table.append(dict(label=label, parent=int(parent), children=children, rows=len(rows), reasons=rows.estimateReason.tolist(), listed=len(got), estimated=est))
    res.tables["quoted_estimates"] = table


# ----------------------------------------------------------------------------------------------------
# G. (Y1-A) events export: raw-unit published events -> split_adj engine units, re-anchored on the export panel.
# ----------------------------------------------------------------------------------------------------
EVENTS_EXPORT_PAIRS = [
    # (label, target id, successor id): the four published rows the raw-unit file failed the guard on in a split_adj panel
    ("BRCM_to_AVGO", 2595644, 555183874),
    ("TYC_to_JCI", 34969540, 372277281),
    ("EIDP_to_DD", 2607387, 383332720),
    ("QRTEA_to_QVCAQ", 326004249, 580712670),
]


def events_rows_from_export(E: pd.DataFrame, tid: int, symbols: list[str]) -> list[dict]:
    """One engine `events:` row (harness EVENT_TYPES layout) from the export's row of `tid`."""
    r = E[E.tradingItemId == tid].iloc[0]
    succ = str(int(r.successorTradingItemId)) if pd.notna(r.successorTradingItemId) else None
    return [dict(tradingItemId=str(tid), securityEndDate=pd.Timestamp(r.endDate),
                 cashPerShareCache=None if pd.isna(r.cashPerShareCache) else float(r.cashPerShareCache),
                 successorTradingItemId=succ if succ in symbols else succ,
                 successorRatioCache=None if pd.isna(r.successorRatioCache) else float(r.successorRatioCache),
                 hasStockLeg=bool(r.hasStockLeg), hasCashLeg=bool(r.hasCashLeg),
                 successorCacheClose=None if pd.isna(r.successorCacheClose) else float(r.successorCacheClose),
                 successorCacheCloseDate=None if pd.isna(r.successorCacheCloseDate) else pd.Timestamp(r.successorCacheCloseDate),
                 targetCacheMark=None if pd.isna(r.targetCacheMark) else float(r.targetCacheMark),
                 targetCacheMarkDate=None if pd.isna(r.targetCacheMarkDate) else pd.Timestamp(r.targetCacheMarkDate),
                 settlementReturn=None if pd.isna(r.settlementReturn) else float(r.settlementReturn))]


def run_events_pair(args, probe: Probe, res: Results, S: str, label: str, cv, E_adj: pd.DataFrame, E_raw: pd.DataFrame,
                    tid: int, succ: int, out: Path, expect_raw_refused: bool | None):
    """Fixture = split_adj lifecycle-cut closes of (target, successor) from `cv`; events row from the export (must pass the
    guard and settle at the raw-unit return) and from the raw file (refused where a split intervenes)."""
    adj = cv.get_panel("close", "split_adj", cut="lifecycle")
    raw = cv.get_panel("close", "raw", cut="lifecycle")
    days = adj.index
    closes = {str(c): adj[c].to_numpy(np.float64).tolist() for c in adj.columns}
    symbols = sorted(closes)
    sig = {str(tid): [1.0] * len(days)}
    r = E_adj[E_adj.tradingItemId == tid].iloc[0]
    res.info(S, label, "export row", f"status {r.cacheUnitStatus}; settle {pd.Timestamp(r.settlementBarDate).date() if pd.notna(r.settlementBarDate) else None}; "
                                     f"cash {fmt(r.cashPerShareCache)} ratio {fmt(r.successorRatioCache)} sf_T {fmt(r.cacheUnitFactorTarget)} sf_S {fmt(r.cacheUnitFactorSuccessor)}; "
                                     f"mark {fmt(r.targetCacheMark)}@{pd.Timestamp(r.targetCacheMarkDate).date()} succ {fmt(r.successorCacheClose)}")
    res.check(S, label, "export status ok", "ok", str(r.cacheUnitStatus))
    fx = Fixture(name=f"events_{label}", dir=out / "events_export" / label, symbols=symbols, dates=days,
                 close=np.array([closes[s_] for s_ in symbols], dtype=np.float32).T,
                 sig=np.array([sig.get(s_, [np.nan] * len(days)) for s_ in symbols], dtype=np.float32).T,
                 event_rows=events_rows_from_export(E_adj, tid, symbols), warmup=1, labels={s_: s_ for s_ in symbols})
    cfg = fx.write()
    # raw-unit expectation from the source terms and raw closes at the export's bars
    src = E_raw[E_raw.tradingItemId == tid].iloc[0]
    mark_raw = float(raw.at[pd.Timestamp(r.targetCacheMarkDate), tid])
    sc_raw = float(raw.at[pd.Timestamp(r.successorCacheCloseDate), succ]) if pd.notna(r.successorCacheCloseDate) else math.nan
    cash = 0.0 if pd.isna(src.cashPerShareCache) else float(src.cashPerShareCache)
    ratio = float(src.successorRatioCache) if pd.notna(src.successorRatioCache) else math.nan
    R_raw = (cash + (ratio * sc_raw if np.isfinite(ratio) else 0.0)) / mark_raw - 1.0
    for mode in (1, 0):
        engine = probe.run(cfg, mode, fx.dir / f"engine_D{mode}.json")
        res.check(S, label, f"D{mode} engine accepts the split_adj events export (guard)", "ok", f"error: {engine['error']}" if "error" in engine else "ok")
        if "error" in engine:
            continue
        res.check(S, label, f"D{mode} eventsPriceChecks == 1", 1, int(engine["eventsPriceChecks"]))
        setl = engine["settlements"]
        res.check(S, label, f"D{mode} one settlement", 1, len(setl))
        if not setl:
            continue
        t_bar = int(setl[0]["bar"])
        res.check(S, label, f"D{mode} settlement bar == export settlementBarDate", str(pd.Timestamp(r.settlementBarDate).date()), str(days[t_bar].date()))
        res.check(S, label, f"D{mode} settlement source computed", "computed", setl[0]["source"])
        res.check(S, label, f"D{mode} settlement return == raw-unit (cash + ratio × raw succ close)/raw mark − 1", R_raw, float(setl[0]["return"]), tol=1e-6)
        if pd.notna(src.settlementReturn):
            res.check(S, label, f"D{mode} settlement return == published settlementReturn", float(src.settlementReturn), float(setl[0]["return"]), tol=1e-6)
        if succ in adj.columns and np.isfinite(ratio):
            res.check(S, label, f"D{mode} successor received", str(succ), str(engine["symbols"][setl[0]["successor"]]) if setl[0]["successor"] >= 0 else "none")
    # negative control: the raw-unit row on the same split_adj panel
    fx_raw = Fixture(name=f"events_{label}_rawfile", dir=out / "events_export" / f"{label}_rawfile", symbols=symbols, dates=days,
                     close=fx.close, sig=fx.sig, event_rows=events_rows_from_export(E_raw.assign(settlementBarDate=pd.NaT), tid, symbols),
                     warmup=1, labels=fx.labels)
    engine = probe.run(fx_raw.write(), 1, fx_raw.dir / "engine_D1.json")
    # the published file is refused as stale (its anchors are raw closes); the synthetic raw file carries no anchors at all
    refused = "error" in engine and ("stale" in engine["error"] or "without price anchors" in engine["error"])
    if expect_raw_refused is not None:
        res.check(S, label, "raw-unit events row on the split_adj panel is refused by the guard", expect_raw_refused, refused)
    res.info(S, label, "raw-file outcome", engine.get("error", "accepted")[:160])
    return dict(label=label, target=tid, successor=succ, status=str(r.cacheUnitStatus), settle=str(pd.Timestamp(r.settlementBarDate).date()) if pd.notna(r.settlementBarDate) else None,
                cash_adj=float(r.cashPerShareCache) if pd.notna(r.cashPerShareCache) else math.nan, ratio_adj=float(r.successorRatioCache) if pd.notna(r.successorRatioCache) else math.nan,
                sf_T=float(r.cacheUnitFactorTarget) if pd.notna(r.cacheUnitFactorTarget) else math.nan, sf_S=float(r.cacheUnitFactorSuccessor) if pd.notna(r.cacheUnitFactorSuccessor) else math.nan,
                R_raw=R_raw, R_file=float(src.settlementReturn) if pd.notna(src.settlementReturn) else math.nan, raw_refused=refused)


def section_events_export(args, probe: Probe, res: Results, out: Path):
    S = "G.events_export"
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from loader import CacheV21
    except Exception as exc:  # pragma: no cover
        res.info(S, "-", "reference loader import failed; section skipped", str(exc))
        return
    table = []
    # 1. synthetic fixture cache (make_fixture 'ok' + the synthetic events file of test_loader): 105 -> 101 (successor splits
    #    3:2 after the settlement -> ratio 0.5 -> 0.75), 104 -> 102 cash 80 + 0.8 (identity)
    try:
        from make_fixture import make_fixture
        from test_loader import _events_fixture
        cache_root = out / "events_export" / "synthetic_cache"
        if not (cache_root / "base_data.parquet").exists():
            make_fixture(cache_root, "ok")
        _events_fixture(cache_root)
        cv = CacheV21(cache_root)
        m = cv.export_events(out / "events_export" / "synthetic_events_cache.parquet")
        res.check(S, "synthetic", "export status counts", json.dumps({"ok": 2, "no_settlement_bar_in_panel": 1, "target_not_in_panel": 1, "no_terms": 1}, sort_keys=True),
                  json.dumps(m["status"], sort_keys=True))
        E_adj = pd.read_parquet(out / "events_export" / "synthetic_events_cache.parquet")
        E_raw = pd.read_parquet(cache_root / "events" / "corporate_actions.parquet")
        for label, tid, succ in (("synthetic_105_to_101_split_after", 105, 101), ("synthetic_104_to_102_mixed", 104, 102)):
            table.append(run_events_pair(args, probe, res, S, label, cv, E_adj, E_raw, tid, succ, out, expect_raw_refused=True))   # the synthetic raw file carries no anchors
    except Exception as exc:
        res.check(S, "synthetic", "synthetic events export", "ok", f"error: {exc}")
    # 2. the four published pairs, each on a small (target, successor) panel exported from the cache root
    root = Path(args.cache_root)
    if not (root / "events" / "corporate_actions.parquet").exists():
        res.info(S, "-", "cache root has no events file; real pairs skipped", str(root))
        res.tables["events_export"] = table
        return
    E_raw = pd.read_parquet(root / "events" / "corporate_actions.parquet")
    for label, tid, succ in EVENTS_EXPORT_PAIRS:
        try:
            end = pd.Timestamp(E_raw.loc[E_raw.tradingItemId == tid, "endDate"].iloc[0])
            cv = CacheV21(root, master_root=root, ids=[tid, succ], start=(end - pd.Timedelta(days=45)).strftime("%Y-%m-%d"),
                          end=(end + pd.Timedelta(days=30)).strftime("%Y-%m-%d"))
            m = cv.export_events(out / "events_export" / f"{label}_events_cache.parquet")
            E_adj = pd.read_parquet(out / "events_export" / f"{label}_events_cache.parquet")
            row = run_events_pair(args, probe, res, S, label, cv, E_adj, E_raw, tid, succ, out, expect_raw_refused=None)
            table.append(row)
        except Exception as exc:
            res.check(S, label, "events export pair", "ok", f"error: {exc}")
    # the raw file must be refused on at least the pairs where a split intervenes (AVGO 10:1 2024, QVCA.Q 1:50 2025, DD 9:1 2019)
    refused = {r["label"]: r["raw_refused"] for r in table if r["label"] in dict((l_, 0) for l_, _, _ in EVENTS_EXPORT_PAIRS)}
    for label in ("BRCM_to_AVGO", "QRTEA_to_QVCAQ", "EIDP_to_DD"):
        if label in refused:
            res.check(S, label, "raw-unit row refused on the split_adj panel (split after the settlement)", True, refused[label])
    res.tables["events_export"] = table


# ----------------------------------------------------------------------------------------------------
# F. HTTP cross-check through longshort-server.
# ----------------------------------------------------------------------------------------------------
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_json(url: str, payload=None, timeout=60):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def section_http(args, probe: Probe, res: Results, real_rows: list[dict]):
    S = "F.http"
    server = probe.server
    if not server.exists():
        res.info(S, "-", "longshort-server not built; HTTP cross-check skipped", str(server))
        return
    for r in real_rows:
        fx = r["fixture"]
        cfg = fx.dir / "config.yaml"
        port = free_port()
        log = open(fx.dir / "server.log", "w")
        env = dict(os.environ, LONGSHORT_PORT=str(port))
        proc = subprocess.Popen([str(server), str(cfg)], cwd=fx.dir, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            config = None
            for _ in range(120):
                if proc.poll() is not None:
                    break
                try:
                    config = http_json(f"http://127.0.0.1:{port}/api/config")
                    break
                except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
                    time.sleep(0.25)
            if config is None:
                res.check(S, fx.name, "server started", "ok", f"failed (see {fx.dir / 'server.log'})")
                continue
            own = json.loads((fx.dir / "engine_D1.json").read_text())
            ca_cfg = config.get("corporateActions", {})
            res.check(S, fx.name, "/api/config corporateActions.legs == probe loader report", int(own["ledgerReport"]["legs"]), int(ca_cfg.get("legs", -1)))
            res.check(S, fx.name, "/api/config corporateActions.priceChecks == probe", int(own["ledgerReport"]["priceChecks"]), int(ca_cfg.get("priceChecks", -1)))
            for mode in (1, 0):
                own = json.loads((fx.dir / f"engine_D{mode}.json").read_text())
                body = http_json(f"http://127.0.0.1:{port}/api/simulate",
                                 {"xml": '<data expr="sig" delay="0"/>', "mode": mode, "distributionLog": True, "settlementLog": True})
                ts = body["timeseries"]
                first = max(fx.warmup, 1) if mode == 1 else fx.warmup
                cum = np.array(ts["cumPnlGross"], dtype=np.float64)
                gross_http = np.diff(np.concatenate([[0.0], cum]))
                gross_probe = np.array([float(b["gross"]) for b in own["bars"]][first:])
                res.check(S, fx.name, f"D{mode} timeseries length == T − first metric bar", len(gross_probe), len(gross_http))
                if len(gross_probe) == len(gross_http):
                    res.check(S, fx.name, f"D{mode} max |Δ cumPnlGross-derived gross vs probe gross|", 0.0, float(np.max(np.abs(gross_http - gross_probe))), tol=1e-12)
                    net_probe = np.array([float(b["ret"]) for b in own["bars"]][first:])
                    cum_net = np.array(ts["cumPnl"], dtype=np.float64)
                    res.check(S, fx.name, f"D{mode} max |Δ cumPnl-derived net vs probe ret|", 0.0, float(np.max(np.abs(np.diff(np.concatenate([[0.0], cum_net])) - net_probe))), tol=1e-12)
                    div = np.array(ts["dividendPnl"], dtype=np.float64)
                    dist = np.array(ts["distributionPnl"], dtype=np.float64)
                    res.check(S, fx.name, f"D{mode} timeseries.dividendPnl == probe", 0.0, float(np.max(np.abs(div - np.array([float(b["corporateActions"]["dividendPnl"]) for b in own["bars"]][first:])))), tol=0.0)
                    res.check(S, fx.name, f"D{mode} timeseries.distributionPnl == probe", 0.0, float(np.max(np.abs(dist - np.array([float(b["corporateActions"]["distributionPnl"]) for b in own["bars"]][first:])))), tol=0.0)
                ca = body["corporateActions"]
                res.check(S, fx.name, f"D{mode} corporateActions.unconfirmed.count == probe records", len(own["unconfirmed"]), int(ca["unconfirmed"]["count"]))
                res.check(S, fx.name, f"D{mode} corporateActions.pnlUnconfirmed == (count > 0)", len(own["unconfirmed"]) > 0, bool(ca["pnlUnconfirmed"]))
                if len(gross_probe) == len(gross_http):
                    unc = np.array(ts["unconfirmedWeight"], dtype=np.float64)
                    res.check(S, fx.name, f"D{mode} timeseries.unconfirmedWeight == probe", 0.0, float(np.max(np.abs(unc - np.array([float(b["corporateActions"]["unconfirmedAbsWeight"]) for b in own["bars"]][first:])))), tol=0.0)
                res.check(S, fx.name, f"D{mode} corporateActions.distributionPnl total == probe", sum(float(b["corporateActions"]["distributionPnl"]) for b in own["bars"]), float(ca["distributionPnl"]), tol=1e-15)
                res.check(S, fx.name, f"D{mode} corporateActions.childPositionsCreated == probe", sum(int(b["corporateActions"]["childPositionsCreated"]) for b in own["bars"]), int(ca["childPositionsCreated"]))
                log_entries = body.get("distributionLog", [])
                res.check(S, fx.name, f"D{mode} distributionLog entries == probe records", len(own["distributions"]), len(log_entries))
                for k, (e, p) in enumerate(zip(log_entries, own["distributions"])):
                    res.check(S, fx.name, f"D{mode} distributionLog[{k}] childWeight == probe", float(p["childWeight"]), float(e["childWeight"]), tol=0.0)
                    res.check(S, fx.name, f"D{mode} distributionLog[{k}] parent/child/source", f"{own['symbols'][p['parent']]}/{own['symbols'][p['child']] if p['child'] >= 0 else None}/{p['source']}",
                              f"{e['parent']}/{e.get('child')}/{e['source']}")
                if "settlementLog" in body:
                    res.check(S, fx.name, f"D{mode} settlementLog entries == probe", len(own["settlements"]), len(body["settlementLog"]))
                    for k, (e, p) in enumerate(zip(body["settlementLog"], own["settlements"])):
                        res.check(S, fx.name, f"D{mode} settlementLog[{k}] return == probe", float(p["return"]), float(e["return"]), tol=0.0)
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
            log.close()


# ----------------------------------------------------------------------------------------------------
# Report.
# ----------------------------------------------------------------------------------------------------
def fmt(x, digits=12):
    if isinstance(x, (bool, str)):
        return str(x)
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "nan"
    if isinstance(x, (int, np.integer)):
        return str(int(x))
    return f"{float(x):.{digits}g}"


def write_report(args, res: Results, probe: Probe, out: Path, t0: float) -> Path:
    lines = ["# recon_engine_ca — engine corporate-action P&L reconciliation (generated)", ""]
    n_ok = sum(1 for c in res.checks if c["ok"] and not c.get("info"))
    n_fail = len(res.failures)
    n_info = sum(1 for c in res.checks if c.get("info"))
    src_hashes = {f: sha256(probe.source / "src" / f)[:12] for f in ("engine/simulator.cpp", "engine/simulator.h", "data/parquet_reader.cpp") if (probe.source / "src" / f).exists()}
    lines += [f"- build: `{probe.build}` (source `{probe.source}`, libengine.a {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(probe.lib.stat().st_mtime))}; "
              f"sha256[:12] {', '.join(f'{k} {v}' for k, v in src_hashes.items())})",
              f"- checks: **{n_ok} pass / {n_fail} fail** (+{n_info} informational), tolerance {args.tol:g}",
              f"- inputs: cache `{args.cache_root}`, gs `{args.gs}`, ledger `{args.ledger}`, returns_v2 `{args.returns_v2}`",
              f"- runtime {time.time() - t0:.1f}s, out `{out}`", ""]
    if res.failures:
        lines += ["## Failures", "", "| section | case | check | expected | actual | diff | tol |", "|---|---|---|---|---|---|---|"]
        for c in res.failures:
            lines.append(f"| {c['section']} | {c['case']} | {c['name']} | {fmt(c['expected'])} | {fmt(c['actual'])} | {fmt(c['diff'], 3)} | {c['tol']} |")
        lines.append("")
    if "real_dist" in res.tables:
        lines += ["## B.1 Real distribution cases (ledger path)", "",
                  "| case | parent→child | ex | w | ratioCache | prev f32 | ex f32 | child f32 | ret_cash_style (f32 marks) | ret GS (decimal) | engine D1 P&L | engine D0 P&L | D1 − w·ret_f32 | D1 − w·ret_GS | child w D1 |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in res.tables["real_dist"]:
            lines.append(f"| {r['case']} | {r['ticker']} | {r['ex']} | {r['w']} | {fmt(r['ratio_cache'])} | {fmt(r['prev_f32'])} | {fmt(r['ex_f32'])} | {fmt(r['child_f32'])} | "
                         f"{fmt(r['ret_f32'])} | {fmt(r['ret_gs'])} | {fmt(r.get('engine_D1'))} | {fmt(r.get('engine_D0'))} | {fmt(r.get('diff_f32_D1'), 3)} | {fmt(r.get('diff_gs_D1'), 3)} | {fmt(r.get('child_w_D1'))} |")
        lines.append("")
    if "real_merger" in res.tables:
        lines += ["## B.2 Real merger cases (events path)", "",
                  "| case | target→acquirer | w | end date (last close) | engine settlement bar | GS ex date | mark | cash | ratio | acquirer close (engine bar) | acquirer close (GS date) | R expected | R engine D1 | R engine D0 | R GS | D1 − GS | R with rc1 k-anchored row |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in res.tables["real_merger"]:
            lines.append(f"| {r['case']} | {r['ticker']} | {r['w']} | {r['end_date']} | {r['settle']} | {r['gs_ex']} | {fmt(r['mark'])} | {fmt(r['cash'])} | {fmt(r['ratio'])} | "
                         f"{fmt(r['succ_close'])} | {fmt(r['succ_close_gs_date'])} | {fmt(r['R_expected'])} | {fmt(r.get('R_engine_D1'))} | {fmt(r.get('R_engine_D0'))} | {fmt(r['R_gs'])} | {fmt(r.get('diff_gs_D1'), 3)} | {fmt(r.get('R_rc1_D1'))} |")
        lines.append("")
    if "split_path" in res.tables:
        lines += ["## C2 Split-path transitions: events settlement + ledger child leg on the same parent and bar", "",
                  "| case | mode | contract total (w × ((cash + Σ ratio×child)/mark − 1)) | engine settlement-bar gross | diff | events R | ledger children booked | legsOnSettlementBar | legsDeferredToEvents | default used | legsAfterParentEnd (loader) |",
                  "|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in res.tables["split_path"]:
            lines.append(f"| {r['case']} | D{r['mode']} | {fmt(r['contract'])} | {fmt(r['engine'])} | {fmt(r['engine'] - r['contract'], 3)} | {fmt(r['events_R'])} | {r['ledger_booked']} | {r.get('on_bar')} | {r.get('deferred')} | {r['default_used']} | {r['legs_after_end']} |")
        lines.append("")
    if "double_count" in res.tables:
        lines += ["## D Double counting: engine-with-ledger ex-date P&L vs w × returns_total v2", "",
                  "| case | w | returns_total v2 | retFlag | w × ret_v2 (no-ledger engine on returns_total) | engine-with-ledger D1 | engine D0 | D1 − w·ret_v2 | ledger child value (would be double counted) |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for r in res.tables["double_count"]:
            lines.append(f"| {r['case']} | {r['w']} | {fmt(r['ret_v2'])} | {r['retFlag']} | {fmt(r['no_ledger_pnl'])} | {fmt(r.get('engine_D1'))} | {fmt(r.get('engine_D0'))} | {fmt(r.get('diff_D1'), 3)} | {fmt(r.get('ledger_component_D1'))} |")
        lines.append("")
    if "export" in res.tables:
        lines += ["## E R1 export identity", "",
                  "| case | R1 childRatioCache | implied raw ratio | R1 parentCloseBefore | raw prev | sf_parent(prev) | R1 parentCloseAtEx | R1 childCloseAtEx | ret (cache units) | ret (raw units) | R1 legs matched D1 | rowsQuantity |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in res.tables["export"]:
            lines.append(f"| {r['case']} | {fmt(r['ratio_cache_R1'])} | {fmt(r['ratio_raw'])} | {fmt(r['before_R1'])} | {fmt(r['raw_prev'])} | {r['sf_parent_prev']} | {fmt(r['ex_R1'])} | {fmt(r['child_R1'])} | {fmt(r['ret_cache'])} | {fmt(r['ret_raw'])} | {r.get('R1_legs_D1')} | {r.get('R1_rowsQuantity_D1')} |")
        lines.append("")
    if "quoted_estimates" in res.tables:
        lines += ["## E2 Quote-priced rows flagged as estimates (held parent -> unconfirmed estimate_value(child_close:<reason>))", "",
                  "| parent @ ex-date | children | flagged rows | reasons | listed on the ex bar | distributionEstimated |", "|---|---|---|---|---|---|"]
        for r in res.tables["quoted_estimates"]:
            lines.append(f"| {r['label']} | {r['children']} | {r['rows']} | {r['reasons']} | {r['listed']} | {r['estimated']} |")
        lines.append("")
    if "events_export" in res.tables:
        lines += ["## G Events export (raw-unit published file -> split_adj engine units)", "",
                  "| case | target | successor | status | settlement bar | cash_adj | ratio_adj | sf_T | sf_S | R raw-unit | R published | raw row refused |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in res.tables["events_export"]:
            lines.append(f"| {r['label']} | {r['target']} | {r['successor']} | {r['status']} | {r['settle']} | {fmt(r['cash_adj'])} | {fmt(r['ratio_adj'])} | {fmt(r['sf_T'])} | {fmt(r['sf_S'])} | {fmt(r['R_raw'])} | {fmt(r['R_file'])} | {r['raw_refused']} |")
        lines.append("")
    lines += ["## All checks", "", "| ok | section | case | check | expected | actual | diff | note |", "|---|---|---|---|---|---|---|---|"]
    for c in res.checks:
        mark = "info" if c.get("info") else ("PASS" if c["ok"] else "**FAIL**")
        lines.append(f"| {mark} | {c['section']} | {c['case']} | {c['name']} | {fmt(c['expected'])} | {fmt(c['actual'])} | {fmt(c['diff'], 3) if c['diff'] != '' else ''} | {c['note']} |")
    path = out / "recon_report.md"
    path.write_text("\n".join(lines) + "\n")
    (out / "recon_checks.json").write_text(json.dumps([{k: (v if not isinstance(v, (np.floating, np.integer)) else float(v)) for k, v in c.items()} for c in res.checks], indent=1, default=str))
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build-dir", type=Path, required=True, help="CMake build dir with libengine.a, compile_commands.json, longshort-server")
    ap.add_argument("--ledger", type=Path, default=DEFAULTS["ledger"], help="R1 engine/ca_ledger_cache.parquet (section E)")
    ap.add_argument("--out", type=Path, default=DEFAULTS["out"])
    ap.add_argument("--cache-root", type=Path, default=DEFAULTS["cache_root"], help="rc1 cache root: base_data.parquet, factors/, master/, calendar.parquet")
    ap.add_argument("--gs", type=Path, default=DEFAULTS["gs"], help="gs_expected.csv")
    ap.add_argument("--returns-v2", type=Path, default=DEFAULTS["returns_v2"], help="R2 derived/returns_total.parquet (section D)")
    ap.add_argument("--tol", type=float, default=1e-9)
    ap.add_argument("--rebuild", action="store_true", help="cmake --build the engine/server targets before compiling the probe")
    ap.add_argument("--http", action="store_true", help="also cross-check through longshort-server on a private port")
    ap.add_argument("--skip-synthetic", action="store_true")
    args = ap.parse_args(argv)
    t0 = time.time()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    res = Results(args.tol)
    probe = Probe(args.build_dir, out / "probe", args.rebuild)
    cache = Cache(args.cache_root)
    gs = pd.read_csv(args.gs)
    real_rows = section_real_distributions(args, cache, probe, res, gs, out)
    section_real_mergers(args, cache, probe, res, gs, out)
    if not args.skip_synthetic:
        section_synthetic(args, probe, res, out)
        section_split_path(args, probe, res, out)
    section_real_slm(args, cache, probe, res, out)
    section_double_count(args, res, real_rows)
    section_export_identity(args, cache, probe, res, real_rows, out)
    section_events_export(args, probe, res, out)
    if args.http:
        section_http(args, probe, res, real_rows + res.tables.get("real_merger", []))
    report = write_report(args, res, probe, out, t0)
    n_fail = len(res.failures)
    n_ok = sum(1 for c in res.checks if c["ok"] and not c.get("info"))
    print(f"recon_engine_ca: {n_ok} pass / {n_fail} fail — report {report}")
    for c in res.failures:
        print(f"  FAIL [{c['section']}] {c['case']}: {c['name']} expected {fmt(c['expected'])} actual {fmt(c['actual'])} diff {fmt(c['diff'], 3)}")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
