#!/home/jgo/usr/venv-3.11/bin/python
"""cache_v21/master_aux.py - rc2 / R3: auxiliary instruments, aux lifecycle rows, lifecycle date side table.

Auxiliary instruments = trading items outside the symbol set (master/instruments) that a corporate-action leg refers to,
so that the successor / child can be valued (BRIEF rc2 P12 "승계사 밖 122건은 보조 종목 추가 수집", 3-1 childTradingItemId
"base_data 또는 aux 에 있어야 평가 가능"):
  events_successor      events/corporate_actions.parquet successorTradingItemId
  lineage_successor     master/lineage.parquet newId (v1 + v2)
  ledger_child          events/ca_ledger.parquet childTradingItemId (R1; used when the file exists)
  distribution_child    distributions/distributions.parquet (proto v2) kind=stock newTradingItemId
  lineage_predecessor   oldId outside the set whose line continues inside it (TEVA ADR 2657569 -> 7727657)

Outputs (stage R3/):
  master/aux_instruments.parquet   tradingItemId, reason ('+'-joined), referencedBy, firstNeededDate, nReferences, symbol, companyName,
                                   exchangeId, tiCurrencyId, securitySubTypeId, tradingItemStatusId, securityStartDate, securityEndDate,
                                   priceFetch ('usd' | 'skipped_non_usd' | 'skipped_no_identity')
  base_data_aux.parquet            prices_build.build_ids(...) for the 'usd' rows: same schema as base_data, since 2000-01-01, <= asOf
  master/lifecycle_aux.parquet     LIFECYCLE_COLS rows for the aux ids, status='aux' (never tradable, never a universe candidate)
  master/lifecycle_dates.parquet   side table for every confirmed instrument: lastTradeDate (lifecycle), effectiveDate (corporate
                                   action end/effective date) and settlementDate/payDate kept as separate columns, each with a source
  qa/aux_coverage.csv              per aux id: rows, first/last close, firstNeededDate covered?, reasons

publish (lead) appends base_data_aux rows to base_data (same schema; ids disjoint from the symbol set; sort (tradingItemId, date)
per row group is preserved because aux ids get their own row groups) and lifecycle_aux rows to lifecycle; universe/loader treat
status 'aux' as not tradable (see R3 report section 3).

CLI:
  master_aux.py collect  --stage <R3 stage> [--ledger <ca_ledger.parquet>] [--lineage-v2 <lineage.parquet>]
  master_aux.py prices   --stage <R3 stage> --work <work dir> [--asof 2026-09-15]     (calls prices_build.build_ids)
  master_aux.py lifecycle --stage <R3 stage>                                            (aux lifecycle rows + coverage)
  master_aux.py dates    --stage <R3 stage>                                             (lifecycle_dates side table)
  master_aux.py factors  --stage <R3 stage> --work <work dir>       (factors_aux/{split,dividend}.parquet via prices_factors)
  master_aux.py dividends --stage <R3 stage> [--append-to for_R1_events.csv]  (ciqdividend cash rows of the aux ids for R1)
  master_aux.py merge --base <base_data.parquet> --out <merged.parquet>       (publish: globally sorted base_data + aux)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from db import DB, chunked, SCHEMA  # noqa: E402
import master_build as mb  # noqa: E402
import master_lifecycle as ml  # noqa: E402

log = logging.getLogger("cache_v21.aux")

SCRATCH = mb.SCRATCH
RC1_ROOT = Path("/home/jgo/61/data2/temp_stock/cache_v2.1_cash_1d")
DEFAULT_STAGE = SCRATCH / "stage_rc2" / "R3"
USD = 160
AUX_STATUS = "aux"
NEEDED_LOOKBACK_DAYS = 45           # when-issued / settlement-bar guard window before the referencing date
AUX_COLS = ["tradingItemId", "reason", "referencedBy", "firstNeededDate", "nReferences", "symbol", "companyName", "exchangeId",
            "tiCurrencyId", "securitySubTypeId", "tradingItemStatusId", "securityStartDate", "securityEndDate", "priceFetch"]


# --------------------------------------------------------------------------------------
# collect
# --------------------------------------------------------------------------------------
def _refs(frame: pd.DataFrame, id_col: str, parent_col: str, date_col: str, reason: str) -> pd.DataFrame:
    f = frame[frame[id_col].notna()].copy()
    if f.empty:
        return pd.DataFrame(columns=["tradingItemId", "reason", "ref", "date"])
    return pd.DataFrame({"tradingItemId": f[id_col].astype("int64").to_numpy(), "reason": reason,
                         "ref": [f"{reason}:{int(p)}@{pd.Timestamp(d).date() if pd.notna(d) else ''}" for p, d in zip(f[parent_col], f[date_col])],
                         "date": pd.to_datetime(f[date_col]).to_numpy()})


def collect_references(instruments: pd.DataFrame, events: pd.DataFrame | None, lineage: pd.DataFrame | None,
                       ledger: pd.DataFrame | None, distributions: pd.DataFrame | None,
                       extra: list[dict] | None = None) -> pd.DataFrame:
    """One row per (aux id, reference); aux id = referenced id not in instruments."""
    inset = set(int(i) for i in instruments.tradingItemId)
    parts = []
    if events is not None and len(events):
        parts.append(_refs(events, "successorTradingItemId", "tradingItemId", "endDate", "events_successor"))
    if lineage is not None and len(lineage):
        parts.append(_refs(lineage, "newId", "oldId", "effectiveDate", "lineage_successor"))
        # a predecessor outside the set whose successor is inside (TEVA ADR): keep the old line so the chain has a prev close
        pred = lineage[lineage.newId.isin(list(inset)) & ~lineage.oldId.isin(list(inset))]
        if len(pred):
            parts.append(_refs(pred.rename(columns={"oldId": "_id", "newId": "_p"}), "_id", "_p", "effectiveDate", "lineage_predecessor"))
    if ledger is not None and len(ledger):
        parts.append(_refs(ledger, "childTradingItemId", "tradingItemId", "exDate", "ledger_child"))
    if distributions is not None and len(distributions):
        d = distributions[(distributions.kind == "stock") & distributions.newTradingItemId.notna()]
        parts.append(_refs(d, "newTradingItemId", "tradingItemId", "exDate", "distribution_child"))
    for e in extra or []:
        parts.append(pd.DataFrame([{"tradingItemId": int(e["tradingItemId"]), "reason": e["reason"], "ref": e["ref"],
                                    "date": pd.Timestamp(e["date"])}]))
    refs = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=["tradingItemId", "reason", "ref", "date"])
    refs["tradingItemId"] = refs.tradingItemId.astype("int64")
    refs = refs[~refs.tradingItemId.isin(list(inset))].drop_duplicates(["tradingItemId", "ref"]).reset_index(drop=True)
    return refs


def identity(db: DB, ids: list[int]) -> pd.DataFrame:
    parts = []
    for ch in chunked(sorted(set(int(i) for i in ids))):
        parts.append(db.query(f"""
            select ti.tradingitemid as "tradingItemId", ti.tickersymbol as symbol, c.companyname as "companyName", ti.exchangeid as "exchangeId",
                   ti.currencyid as "tiCurrencyId", s.securitysubtypeid as "securitySubTypeId", ti.tradingitemstatusid as "tradingItemStatusId",
                   s.securitystartdate as "securityStartDate", s.securityenddate as "securityEndDate", s.securityid as "securityId", s.companyid as "companyId"
            from {SCHEMA}.ciqtradingitem ti join {SCHEMA}.ciqsecurity s on s.securityid = ti.securityid
            left join {SCHEMA}.ciqcompany c on c.companyid = s.companyid where ti.tradingitemid = any(%s)""", (ch,)))
    if not parts:
        return pd.DataFrame(columns=["tradingItemId"])
    df = pd.concat(parts, ignore_index=True)
    for c in ("securityStartDate", "securityEndDate"):
        df[c] = mb.to_ns(df[c])
    return df


def build_aux_instruments(refs: pd.DataFrame, ident: pd.DataFrame) -> pd.DataFrame:
    g = refs.sort_values(["tradingItemId", "date"]).groupby("tradingItemId")
    aux = pd.DataFrame({
        "tradingItemId": np.array(list(g.groups.keys()), dtype=np.int64),
        "reason": g.reason.agg(lambda s: "+".join(sorted(set(s)))).to_numpy(),
        "referencedBy": g.ref.agg(lambda s: ";".join(list(s)[:12]) + (f";+{len(s) - 12}" if len(s) > 12 else "")).to_numpy(),
        "firstNeededDate": (g.date.min() - pd.Timedelta(days=NEEDED_LOOKBACK_DAYS)).to_numpy(),
        "nReferences": g.size().to_numpy(),
    })
    aux = aux.merge(ident.drop(columns=[c for c in ("securityId", "companyId") if c in ident.columns]), on="tradingItemId", how="left")
    has_id = aux.symbol.notna() | aux.companyName.notna() | aux.exchangeId.notna()
    aux["priceFetch"] = np.where(~has_id, "skipped_no_identity", np.where(aux.tiCurrencyId.fillna(-1).astype(int) == USD, "usd", "skipped_non_usd"))
    aux["firstNeededDate"] = mb.to_ns(aux.firstNeededDate)
    for c in ("exchangeId", "tiCurrencyId", "securitySubTypeId", "tradingItemStatusId"):
        aux[c] = aux[c].astype("Int64")
    return aux[AUX_COLS].sort_values("tradingItemId").reset_index(drop=True)


def cmd_collect(args) -> None:
    stage = Path(args.stage)
    (stage / "master").mkdir(parents=True, exist_ok=True)
    (stage / "qa").mkdir(parents=True, exist_ok=True)
    inst = pd.read_parquet(args.instruments)
    ev = pd.read_parquet(args.events) if args.events and Path(args.events).exists() else None
    lin = pd.read_parquet(args.lineage) if args.lineage and Path(args.lineage).exists() else None
    if args.lineage_v2 and Path(args.lineage_v2).exists():
        lin = pd.concat([lin, pd.read_parquet(args.lineage_v2)[["oldId", "newId", "effectiveDate"]]], ignore_index=True) if lin is not None else pd.read_parquet(args.lineage_v2)
    led = pd.read_parquet(args.ledger) if args.ledger and Path(args.ledger).exists() else None
    dist = pd.read_parquet(args.distributions, columns=["tradingItemId", "exDate", "kind", "newTradingItemId"]) if args.distributions and Path(args.distributions).exists() else None
    refs = collect_references(inst, ev, lin, led, dist)
    db = DB(application_name="cache_v21_R3_aux")
    ident = identity(db, refs.tradingItemId.unique().tolist())
    db.close()
    aux = build_aux_instruments(refs, ident)
    mb.write_parquet(aux, stage / "master" / "aux_instruments.parquet")
    refs.to_csv(stage / "qa" / "aux_references.csv", index=False)
    summary = {"n_references": int(len(refs)), "n_aux_ids": int(len(aux)), "by_reason": aux.reason.value_counts().to_dict(),
               "by_source": refs.reason.value_counts().to_dict(), "priceFetch": aux.priceFetch.value_counts().to_dict(),
               "inputs": {"events": str(args.events) if ev is not None else None, "lineage": str(args.lineage) if lin is not None else None,
                          "lineage_v2": str(args.lineage_v2) if args.lineage_v2 and Path(args.lineage_v2).exists() else None,
                          "ledger": str(args.ledger) if led is not None else None, "distributions": str(args.distributions) if dist is not None else None},
               "at": mb.now()}
    mb.save_json(summary, stage / "qa" / "aux_collect_summary.json")
    log.info("aux instruments: %s", {k: v for k, v in summary.items() if k != "inputs"})


# --------------------------------------------------------------------------------------
# prices
# --------------------------------------------------------------------------------------
def cmd_prices(args) -> None:
    import prices_build as pb

    stage, work = Path(args.stage), Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    aux = pd.read_parquet(stage / "master" / "aux_instruments.parquet")
    ids = sorted(set(aux.loc[aux.priceFetch == "usd", "tradingItemId"].astype(int)))
    db = DB(application_name="cache_v21_R3_aux")
    info = pb.build_ids(db, ids, stage / "base_data_aux.parquet", work, args.start, args.asof, args.chunk_size)
    db.close()
    info["coverage"].to_parquet(work / "base_data_aux_coverage.parquet", index=False)
    mb.save_json({k: v for k, v in info.items() if k != "coverage"}, stage / "qa" / "aux_prices_info.json")


# --------------------------------------------------------------------------------------
# lifecycle rows for aux ids (+ coverage report)
# --------------------------------------------------------------------------------------
def aux_lifecycle(aux: pd.DataFrame, coverage: pd.DataFrame, as_of_price: pd.Timestamp, observed_at: pd.Timestamp) -> pd.DataFrame:
    """LIFECYCLE_COLS rows for the aux ids: status 'aux' always (not tradable), dates from the pulled ciq closes; endReason from the
    trading item status / securityEndDate (same vocabulary as lifecycle), 'active' when still quoted within 10 trading days."""
    c = coverage.set_index("tradingItemId").reindex(aux.tradingItemId)
    last = c.lastCloseDate.to_numpy()
    first = c.firstCloseDate.to_numpy()
    sec_end = aux.securityEndDate.to_numpy()
    status_id = aux.tradingItemStatusId
    reason = np.full(len(aux), "unknown", dtype=object)
    reason[(status_id.isin([4, 5, 11]).fillna(False)).to_numpy() | pd.notna(sec_end)] = "delist_other"
    reason[status_id.isin([2]).fillna(False).to_numpy()] = "bankruptcy"
    reason[(status_id == 8).fillna(False).to_numpy()] = "merger"
    recent = pd.Series(last).notna() & (pd.Series(last) >= as_of_price - pd.Timedelta(days=14))
    reason[recent.to_numpy() & ~(status_id.isin([2, 4, 5, 8, 11]).fillna(False).to_numpy())] = "active"
    reason[pd.isna(last)] = "data_gap"
    lc = pd.DataFrame({
        "tradingItemId": aux.tradingItemId.astype("int64").to_numpy(),
        "firstTradeDate": first, "firstPriceDate": first, "firstTradeSource": np.where(pd.isna(first), "", "ciq"),
        "ciqLastCloseDate": last, "miLastCloseDate": pd.NaT, "securityEndDate": sec_end,
        "tradingItemStatusId": pd.array(status_id.to_numpy(dtype=object), dtype="Int64"),
        "eventEndDate": pd.NaT, "lastTradeDate": last, "lastTradeSource": np.where(pd.isna(last), "", "ciq"),
        "endReason": reason, "status": AUX_STATUS,
        "observedAt": pd.Series(pd.Timestamp(observed_at), index=aux.index).astype("datetime64[ns]").to_numpy(),
        "evidence": ["aux:" + r + f" priceFetch={p} nCloseRows={int(n) if pd.notna(n) else 0}" for r, p, n in zip(aux.reason, aux.priceFetch, c.nCloseRows)],
    })
    for col in ("firstTradeDate", "firstPriceDate", "ciqLastCloseDate", "miLastCloseDate", "securityEndDate", "eventEndDate", "lastTradeDate"):
        lc[col] = mb.to_ns(lc[col])
    return lc[ml.LIFECYCLE_COLS].sort_values("tradingItemId").reset_index(drop=True)


def aux_coverage(aux: pd.DataFrame, coverage: pd.DataFrame, cal: np.ndarray) -> pd.DataFrame:
    c = coverage.set_index("tradingItemId").reindex(aux.tradingItemId).reset_index()
    out = aux[["tradingItemId", "symbol", "companyName", "exchangeId", "tiCurrencyId", "reason", "nReferences", "firstNeededDate", "priceFetch"]].copy()
    out["nRows"] = c.nRows.fillna(0).astype(int).to_numpy()
    out["nCloseRows"] = c.nCloseRows.fillna(0).astype(int).to_numpy()
    out["firstCloseDate"] = c.firstCloseDate.to_numpy()
    out["lastCloseDate"] = c.lastCloseDate.to_numpy()
    need = out.firstNeededDate + pd.Timedelta(days=NEEDED_LOOKBACK_DAYS)     # the referencing date itself
    # covered = a close within 30 trading days on or after the referencing date (settlement bar / ex-date valuation window)
    lo = np.where(pd.notna(out.firstCloseDate), out.firstCloseDate, np.datetime64("NaT")).astype("datetime64[ns]")
    hi = np.where(pd.notna(out.lastCloseDate), out.lastCloseDate, np.datetime64("NaT")).astype("datetime64[ns]")
    horizon = np.array([pd.Timestamp(cal[min(int(np.searchsorted(cal, np.datetime64(d, "ns"), side="left")) + 30, len(cal) - 1)]).to_datetime64()
                        if pd.notna(d) else np.datetime64("NaT") for d in need], dtype="datetime64[ns]")
    covers = pd.notna(lo) & (lo <= horizon) & (hi >= need.to_numpy().astype("datetime64[ns]"))
    # a predecessor line only has to be quoted up to the transition (its last close feeds the successor's first chained return)
    pred = out.reason.str.contains("lineage_predecessor").to_numpy()
    covers = np.where(pred, pd.notna(hi) & (hi >= out.firstNeededDate.to_numpy().astype("datetime64[ns]")), covers)
    out["coversReferenceDate"] = covers.astype(bool)
    out["coverage"] = np.where(out.priceFetch != "usd", out.priceFetch, np.where(out.nCloseRows == 0, "no_close_rows",
                               np.where(out.coversReferenceDate, "ok", "closes_outside_window")))
    return out


def cmd_lifecycle(args) -> None:
    stage, work = Path(args.stage), Path(args.work)
    aux = pd.read_parquet(stage / "master" / "aux_instruments.parquet")
    cov = pd.read_parquet(work / "base_data_aux_coverage.parquet")
    cal = pd.read_parquet(Path(args.calendar)).date.to_numpy(dtype="datetime64[ns]")
    as_of = pd.Timestamp(args.asof)
    lc = aux_lifecycle(aux, cov, as_of, pd.Timestamp(mb.now()).normalize())
    mb.write_parquet(lc, stage / "master" / "lifecycle_aux.parquet")
    rep = aux_coverage(aux, cov, cal)
    rep.to_csv(stage / "qa" / "aux_coverage.csv", index=False)
    summary = {"n_aux": int(len(aux)), "coverage": rep.coverage.value_counts().to_dict(), "by_reason_coverage":
               rep.groupby(["reason", "coverage"]).size().reset_index().rename(columns={0: "n"}).to_dict(orient="records"),
               "lifecycle_endReason": lc.endReason.value_counts().to_dict(), "status_all_aux": bool((lc.status == AUX_STATUS).all()),
               "asOf": str(as_of.date()), "at": mb.now()}
    mb.save_json(summary, stage / "qa" / "aux_lifecycle_summary.json")
    log.info("aux lifecycle: %s", summary["coverage"])


# --------------------------------------------------------------------------------------
# factors (split / dividend intervals) and cash dividends for the aux ids (review F3)
# --------------------------------------------------------------------------------------
CASH_TYPES = (1, 4, 5, 6, 7, 8, 10, 15, 17, 18, 19, 23, 33)     # = ledger_build.CASH_TYPES


def cmd_factors(args) -> None:
    """factors_aux/{split,dividend}.parquet in the factors/*.parquet interval contract, built by prices_factors.build_all on the
    aux ids with prices (split intervals reconciled against work/aux/adjf_long.parquet written by prices_build.build_ids)."""
    import prices_factors as pf

    stage, work = Path(args.stage), Path(args.work)
    aux = pd.read_parquet(stage / "master" / "aux_instruments.parquet")
    cov = pd.read_parquet(work / "base_data_aux_coverage.parquet").set_index("tradingItemId")
    ids = sorted(int(i) for i in aux.loc[aux.priceFetch == "usd", "tradingItemId"] if int(cov.nRows.get(int(i), 0)) > 0)
    tmp_stage = work / "factors_stage"
    db = DB(application_name="cache_v21_R3_aux")
    info = pf.build_all(db, ids, tmp_stage, work)
    db.close()
    out = stage / "factors_aux"
    out.mkdir(parents=True, exist_ok=True)
    for name in ("split", "dividend"):
        df = pd.read_parquet(tmp_stage / "factors" / f"{name}.parquet")
        chk = pf.check_intervals(df, ids=ids)
        if not chk["ok"]:
            raise RuntimeError(f"{name} intervals broken: {chk['violations']}")
        mb.write_parquet(df, out / f"{name}.parquet")
    summary = {"n_ids": len(ids), "split": {k: v for k, v in info["split"].items() if k not in ("check",)},
               "dividend": {k: v for k, v in info["dividend"].items() if k not in ("check",)},
               "split_check": info["split"]["check"]["ok"], "dividend_check": info["dividend"]["check"]["ok"], "at": mb.now()}
    mb.save_json(summary, stage / "qa" / "aux_factors_summary.json")
    log.info("aux factors: split %s dividend %s", summary["split"].get("by_source"), summary["dividend"].get("by_source"))


def aux_dividend_rows(db: DB, ids: list[int], start: str = "2000-01-01") -> pd.DataFrame:
    """ciqdividend cash-type rows of the aux ids in the R1 hand-over columns (gross first, then divAmount; currency ISO code)."""
    parts = []
    for ch in chunked(sorted(set(int(i) for i in ids))):
        parts.append(db.query(f"""select d.dividendid, d.tradingitemid, d.exdate, d.paydate, d.recorddate, d.announceddate, d.divamount, d.netamount,
                                         d.grossamount, d.dividendtypeid, d.currencyid, c.isocode
                                  from {SCHEMA}.ciqdividend d left join {SCHEMA}.ciqcurrency c on c.currencyid = d.currencyid
                                  where d.tradingitemid = any(%s) and d.exdate >= %s""", (ch, start)))
    dv = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if dv.empty:
        return pd.DataFrame(columns=["tradingItemId", "exDate", "eventType", "legType", "cashPerShare", "cashCurrency", "parentFactor",
                                     "childTradingItemId", "childRatio", "childValueSource", "source", "evidence", "status", "isEstimate", "notes"])
    cash = dv[dv.dividendtypeid.isin(CASH_TYPES) & dv.divamount.notna() & (dv.divamount.astype(float) > 0)].copy()
    amt = cash.grossamount.where(cash.grossamount.notna(), cash.divamount).astype(float)
    out = pd.DataFrame({
        "tradingItemId": cash.tradingitemid.astype("int64"), "exDate": pd.to_datetime(cash.exdate),
        "eventType": np.where(cash.dividendtypeid == 4, "special_dividend", "cash_dividend"), "legType": "cash",
        "cashPerShare": amt.to_numpy(), "cashCurrency": cash.isocode.fillna("USD").to_numpy(), "parentFactor": 1.0,
        "childTradingItemId": pd.array([pd.NA] * len(cash), dtype="Int64"), "childRatio": np.nan, "childValueSource": "",
        "source": "ciqdividend(aux)",
        "evidence": [f"ciqdividend id {int(i)} type {int(t)} div {float(d):.6g} gross {'' if pd.isna(g) else float(g)} net {'' if pd.isna(n) else float(n)} pay {'' if pd.isna(p) else pd.Timestamp(p).date()}"
                     for i, t, d, g, n, p in zip(cash.dividendid, cash.dividendtypeid, cash.divamount, cash.grossamount, cash.netamount, cash.paydate)],
        "status": "ok", "isEstimate": False, "notes": "aux instrument (successor/child) dividend; not in the symbol set's ciqdividend pull",
    })
    other = dv[~dv.dividendtypeid.isin(CASH_TYPES)]
    out.attrs["non_cash_rows"] = {int(k): int(v) for k, v in other.dividendtypeid.value_counts().items()}
    return out.sort_values(["tradingItemId", "exDate"]).reset_index(drop=True)


def cmd_dividends(args) -> None:
    stage = Path(args.stage)
    aux = pd.read_parquet(stage / "master" / "aux_instruments.parquet")
    ids = sorted(int(i) for i in aux.loc[aux.priceFetch == "usd", "tradingItemId"])
    db = DB(application_name="cache_v21_R3_aux")
    rows = aux_dividend_rows(db, ids, args.start)
    db.close()
    rows.to_csv(stage / "for_R1_aux_dividends.csv", index=False)
    target = Path(args.append_to) if args.append_to else None
    if target is not None and target.exists():
        base = pd.read_csv(target)
        base = base[base.source != "ciqdividend(aux)"]           # idempotent re-append
        merged = pd.concat([base, rows.assign(exDate=rows.exDate.dt.strftime("%Y-%m-%d"))], ignore_index=True)
        merged.to_csv(target, index=False)
    summary = {"aux_ids": len(ids), "cash_rows": int(len(rows)), "ids_with_cash_rows": int(rows.tradingItemId.nunique()),
               "by_eventType": rows.eventType.value_counts().to_dict(), "by_currency": rows.cashCurrency.value_counts().to_dict(),
               "non_cash_rows_by_type": rows.attrs.get("non_cash_rows", {}), "appended_to": str(target) if target else None, "at": mb.now()}
    mb.save_json(summary, stage / "qa" / "aux_dividends_summary.json")
    log.info("aux dividends: %s", summary)


# --------------------------------------------------------------------------------------
# publish helper: merge aux rows into base_data keeping the global (tradingItemId, date) sort (review F9)
# --------------------------------------------------------------------------------------
def merge_base_data(base: Path, aux: Path, out: Path) -> dict:
    """Stream base_data row groups (ascending id chunks) and insert the aux rows of every id that falls inside or before a
    group's id range into that group (re-sorted by (tradingItemId, date)); aux ids above the last base id form a final group.
    The output keeps the file-global (tradingItemId, date) order the loader checks, row-group statistics included."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    a = pq.read_table(aux).to_pandas().sort_values(["tradingItemId", "date"]).reset_index(drop=True)
    pf = pq.ParquetFile(base)
    writer = pq.ParquetWriter(out, pf.schema_arrow, compression="zstd", write_statistics=True, use_dictionary=False)
    n_rows, n_aux_in, prev_max = 0, 0, -1
    for rg in range(pf.num_row_groups):
        t = pf.read_row_group(rg)
        ids = t.column("tradingItemId").to_numpy()
        g_max = int(ids.max())
        take = a[(a.tradingItemId > prev_max) & (a.tradingItemId <= g_max)]
        if len(take):
            df = pd.concat([t.to_pandas(), take], ignore_index=True).sort_values(["tradingItemId", "date"], kind="mergesort")
            t = pa.Table.from_pandas(df, schema=pf.schema_arrow, preserve_index=False)
            n_aux_in += len(take)
        writer.write_table(t, row_group_size=max(1, t.num_rows))
        n_rows += t.num_rows
        prev_max = g_max
    rest = a[a.tradingItemId > prev_max]
    if len(rest):
        t = pa.Table.from_pandas(rest, schema=pf.schema_arrow, preserve_index=False)
        writer.write_table(t, row_group_size=max(1, t.num_rows))
        n_rows += t.num_rows
        n_aux_in += len(rest)
    writer.close()
    return {"rows": n_rows, "aux_rows": int(len(a)), "aux_rows_placed": n_aux_in, "row_groups": pf.num_row_groups + (1 if len(rest) else 0)}


def cmd_merge(args) -> None:
    info = merge_base_data(Path(args.base), Path(args.aux), Path(args.out))
    log.info("merged base_data: %s", info)
    print(json.dumps(info))


# --------------------------------------------------------------------------------------
# lifecycle_dates side table (P12: 종료일 불일치 184건은 마지막 거래일·효력일·지급일 분리)
# --------------------------------------------------------------------------------------
DATES_COLS = ["tradingItemId", "lastTradeDate", "lastTradeSource", "effectiveDate", "effectiveDateSource", "settlementDate",
              "settlementDateSource", "payDate", "payDateSource", "securityEndDate", "ciqLastCloseDate", "miLastCloseDate",
              "eventEndDate", "actionCode", "gapEffectiveMinusLastTrade", "gapSettlementMinusLastTrade", "mismatchClass", "everTop500"]


def build_lifecycle_dates(lifecycle: pd.DataFrame, events: pd.DataFrame, ledger: pd.DataFrame | None, top500: set[int] | None = None) -> pd.DataFrame:
    """One row per confirmed instrument (events row exists).  The three dates are never merged:
    lastTradeDate      last genuine close (lifecycle; source ciq/mi/event)
    effectiveDate      corporate-action end/effective date: events.endDateOriginal when it is the DB securityEndDate / co_acthist
                       effective date (endDateSource db*/override), else events.endDate (local = last close, i.e. no independent date)
    settlementDate     co_acthist datadate (events.settlementDate) when the action was matched; NaT otherwise
    payDate            ledger payDate of the merger_cash/merger_stock leg on the same line when R1 supplies one"""
    lc = lifecycle.set_index("tradingItemId")
    ev = events.set_index("tradingItemId")
    ids = ev.index.intersection(lc.index)
    e = ev.loc[ids]
    l = lc.loc[ids]
    src = e.endDateSource.fillna("").astype(str)
    eff_db = src.str.startswith("db") | src.eq("override")
    eff = pd.Series(np.where(eff_db, e.endDateOriginal, e.endDate), index=ids).astype("datetime64[ns]")
    eff_src = np.where(src.eq("override"), "override", np.where(eff_db, "ciqsecurity.securityenddate", np.where(src.str.startswith("local"), "lifecycle.lastTradeDate (no independent date)", src)))
    matched = e.actionCode.notna() & e.matchMethod.fillna("none").ne("none")
    settle = pd.Series(np.where(matched, e.settlementDate, np.datetime64("NaT")), index=ids).astype("datetime64[ns]")
    settle_src = np.where(matched, "co_acthist.datadate", "")
    pay = pd.Series(pd.NaT, index=ids, dtype="datetime64[ns]")
    pay_src = np.full(len(ids), "", dtype=object)
    if ledger is not None and len(ledger):
        lg = ledger[ledger.eventType.isin(["merger_cash", "merger_stock"]) & ledger.payDate.notna()].sort_values("exDate").drop_duplicates("tradingItemId", keep="last").set_index("tradingItemId")
        hit = ids.intersection(lg.index)
        pay.loc[hit] = pd.to_datetime(lg.loc[hit, "payDate"]).to_numpy()
        pay_src[ids.get_indexer(hit)] = "ca_ledger.payDate"
    out = pd.DataFrame({
        "tradingItemId": ids.to_numpy(dtype=np.int64),
        "lastTradeDate": l.lastTradeDate.to_numpy(), "lastTradeSource": l.lastTradeSource.to_numpy(),
        "effectiveDate": eff.to_numpy(), "effectiveDateSource": eff_src,
        "settlementDate": settle.to_numpy(), "settlementDateSource": settle_src,
        "payDate": pay.to_numpy(), "payDateSource": pay_src,
        "securityEndDate": l.securityEndDate.to_numpy(), "ciqLastCloseDate": l.ciqLastCloseDate.to_numpy(), "miLastCloseDate": l.miLastCloseDate.to_numpy(),
        "eventEndDate": e.endDate.to_numpy(), "actionCode": e.actionCode.to_numpy(),
    })
    out["gapEffectiveMinusLastTrade"] = (out.effectiveDate - out.lastTradeDate).dt.days.astype("Int64")
    out["gapSettlementMinusLastTrade"] = (out.settlementDate - out.lastTradeDate).dt.days.astype("Int64")
    g = out.gapEffectiveMinusLastTrade.fillna(0).astype(int)
    ge = (e.endDate.to_numpy() - out.lastTradeDate.to_numpy()).astype("timedelta64[D]").astype(int)
    out["mismatchClass"] = np.where(ge > 0, "endDate_after_lastTrade", np.where(ge < 0, "endDate_before_lastTrade", "match"))
    out["everTop500"] = out.tradingItemId.isin(list(top500 or set()))
    for c in ("lastTradeDate", "effectiveDate", "settlementDate", "payDate", "securityEndDate", "ciqLastCloseDate", "miLastCloseDate", "eventEndDate"):
        out[c] = mb.to_ns(out[c])
    return out[DATES_COLS].sort_values("tradingItemId").reset_index(drop=True)


def cmd_dates(args) -> None:
    stage = Path(args.stage)
    lc = pd.read_parquet(args.lifecycle)
    ev = pd.read_parquet(args.events)
    led = pd.read_parquet(args.ledger, columns=["tradingItemId", "exDate", "eventType", "payDate"]) if args.ledger and Path(args.ledger).exists() else None
    top500 = set()
    if args.top500 and Path(args.top500).exists():
        top500 = set(pd.read_csv(args.top500).tradingItemId.astype(int))
    dates = build_lifecycle_dates(lc, ev, led, top500)
    mb.write_parquet(dates, stage / "master" / "lifecycle_dates.parquet")
    after = dates[dates.mismatchClass == "endDate_after_lastTrade"]
    after.to_csv(stage / "qa" / "lifecycle_dates_endDate_after_lastTrade.csv", index=False)
    summary = {"rows": int(len(dates)), "mismatchClass": dates.mismatchClass.value_counts().to_dict(),
               "after_by_effectiveDateSource": after.effectiveDateSource.value_counts().to_dict(),
               "after_by_actionCode": after.actionCode.fillna("(none)").value_counts().to_dict(),
               "after_gap_days": {"median": float(after.gapEffectiveMinusLastTrade.median()) if len(after) else None,
                                  "p75": float(after.gapEffectiveMinusLastTrade.quantile(0.75)) if len(after) else None,
                                  "max": int(after.gapEffectiveMinusLastTrade.max()) if len(after) else None},
               "after_settlementDate_known": int(after.settlementDate.notna().sum()), "after_payDate_known": int(after.payDate.notna().sum()),
               "after_everTop500": int(after.everTop500.sum()), "at": mb.now()}
    mb.save_json(summary, stage / "qa" / "lifecycle_dates_summary.json")
    log.info("lifecycle_dates: %s", summary)


# --------------------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--stage", default=str(DEFAULT_STAGE))
    c.add_argument("--instruments", default=str(RC1_ROOT / "master" / "instruments.parquet"))
    c.add_argument("--events", default=str(RC1_ROOT / "events" / "corporate_actions.parquet"))
    c.add_argument("--lineage", default=str(RC1_ROOT / "master" / "lineage.parquet"))
    c.add_argument("--lineage-v2", default=str(DEFAULT_STAGE / "master" / "lineage.parquet"))
    c.add_argument("--ledger", default=str(SCRATCH / "stage_rc2" / "R1" / "events" / "ca_ledger.parquet"))
    c.add_argument("--distributions", default=str(RC1_ROOT / "distributions" / "distributions.parquet"))
    p = sub.add_parser("prices")
    p.add_argument("--stage", default=str(DEFAULT_STAGE))
    p.add_argument("--work", default=str(DEFAULT_STAGE / "work" / "aux"))
    p.add_argument("--start", default="2000-01-01")
    p.add_argument("--asof", default=None)
    p.add_argument("--chunk-size", type=int, default=1000)
    lf = sub.add_parser("lifecycle")
    lf.add_argument("--stage", default=str(DEFAULT_STAGE))
    lf.add_argument("--work", default=str(DEFAULT_STAGE / "work" / "aux"))
    lf.add_argument("--calendar", default=str(RC1_ROOT / "calendar.parquet"))
    lf.add_argument("--asof", required=True)
    f = sub.add_parser("factors")
    f.add_argument("--stage", default=str(DEFAULT_STAGE))
    f.add_argument("--work", default=str(DEFAULT_STAGE / "work" / "aux"))
    dv = sub.add_parser("dividends")
    dv.add_argument("--stage", default=str(DEFAULT_STAGE))
    dv.add_argument("--start", default="2000-01-01")
    dv.add_argument("--append-to", default=str(DEFAULT_STAGE / "for_R1_events.csv"))
    m = sub.add_parser("merge", help="publish helper: base_data + base_data_aux -> globally (id, date)-sorted file")
    m.add_argument("--base", required=True)
    m.add_argument("--aux", default=str(DEFAULT_STAGE / "base_data_aux.parquet"))
    m.add_argument("--out", required=True)
    d = sub.add_parser("dates")
    d.add_argument("--stage", default=str(DEFAULT_STAGE))
    d.add_argument("--lifecycle", default=str(RC1_ROOT / "master" / "lifecycle.parquet"))
    d.add_argument("--events", default=str(RC1_ROOT / "events" / "corporate_actions.parquet"))
    d.add_argument("--ledger", default=str(SCRATCH / "stage_rc2" / "R1" / "events" / "ca_ledger.parquet"))
    d.add_argument("--top500", default=None, help="csv with tradingItemId of everTop500 ids (optional)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    {"collect": cmd_collect, "prices": cmd_prices, "lifecycle": cmd_lifecycle, "dates": cmd_dates, "factors": cmd_factors,
     "dividends": cmd_dividends, "merge": cmd_merge}[args.cmd](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
