"""Tests for rc2 / R3: master_aux (aux instruments, aux lifecycle, lifecycle_dates), master_lineage reclassification helpers
and lineage v2 assembly, master_qa lineage v2 gates / aux coverage gate.

Run:  /home/jgo/usr/venv-3.11/bin/python cpp-engine/tools/data/cache_v21/tests/test_master_aux.py   (self-runner; pytest also works)
No DB access: every test works on synthetic frames.  env R3_STAGE=<stage dir> additionally runs the gates on the R3 stage files.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

try:
    import pytest
except ImportError:  # self-runner below
    class _P:  # minimal stand-ins so the module imports without pytest
        class mark:
            @staticmethod
            def parametrize(names, cases):
                def deco(f):
                    f._cases = (names, cases)
                    return f
                return deco

            @staticmethod
            def skipif(cond, reason=""):
                def deco(f):
                    f._skip = bool(cond)
                    return f
                return deco

        class _Raises:
            def __init__(self, exc):
                self.exc = exc

            def __enter__(self):
                return self

            def __exit__(self, et, ev, tb):
                if et is None:
                    raise AssertionError(f"expected {self.exc.__name__}")
                return issubclass(et, self.exc)

        @staticmethod
        def raises(exc):
            return _P._Raises(exc)

    pytest = _P()

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import master_aux as ma  # noqa: E402
import master_lineage as mg  # noqa: E402
import master_qa as mq  # noqa: E402

CAL = pd.bdate_range("2000-01-03", "2027-12-31").values.astype("datetime64[ns]")


# --------------------------------------------------------------------------------------
# parse_exchange_ratio
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("text, ratio, kind", [
    ("1-FOR-20 REVERSE STOCK SPLIT; NAME, TICKER AND CUSIP CHANGED AS A RESULT.", 1 / 20, "reverse_split"),
    ("1 FOR 10 REVERSE STOCK SPLIT, NAME.CUSIP AND TICKER CHANGED", 1 / 10, "reverse_split"),
    ("1-FOR-25,000 REVERSE SPLIT; NAME, CUSIP, AND TICKER CHANGED AS A RESULT.", 1 / 25000, "reverse_split"),
    ("1-FOR-3.8031 REVERSE STOCK SPLIT; TICKER AND CUSIP CHANGED AS A RESULT.", 1 / 3.8031, "reverse_split_fractional"),
    (".7194-FOR-1 REVERSE STOCK SPLIT; TICKER, CUSIP, AND NAME CHANGED AS A RESULT.", 0.7194, "reverse_split"),
    ("5-FOR-32 REVERSE STK SPLIT", 5 / 32, "reverse_split"),
    (".6484-FOR-1 STOCK SPLIT.", 0.6484, "split"),
    ("1.936-FOR-1 STK SPLIT", 1.936, "split"),
    ("BANCFIRST IS THE ACCOUNTING SURVIVOR. TERMS: 1.325 SHARES OF UNIZ FOR EVERY SHARE OF BANCFIRST OHIO CORP HELD.", 1.325, "exchange"),
    ("EACH SHARE OF CLASS A COMMON STOCK  CONVERTED INTO ONE SHARE OF COMMON STOCK", 1.0, "exchange"),
    ("REORGANIZATION: FORMATION OF A HOLDING COMPANY (NAME CHANGE) SHAREHOLDERS REC. AAC HOLDINGS SHARES ON A 1-FOR-1 BASIS.", 1.0, "exchange"),
    ("US WEST AND QWEST MERGED TO FORM NEW QWEST.  OLD QWEST TO RESEARCH FILE.", np.nan, ""),
    (None, np.nan, ""),
])
def test_parse_exchange_ratio(text, ratio, kind):
    r, k = mg.parse_exchange_ratio(text)
    assert k == kind
    if np.isnan(ratio):
        assert np.isnan(r)
    else:
        assert abs(r - ratio) < 1e-9


# --------------------------------------------------------------------------------------
# classify_rename_rejections on a synthetic evidence set (no DB)
# --------------------------------------------------------------------------------------
def _px(tid, dates, closes, adjf=1.0):
    return pd.DataFrame({"tradingitemid": tid, "pricingdate": pd.to_datetime(dates), "raw_close": closes,
                         "adjustmentfactor": adjf, "raw_volume": 1000.0})


def _evidence(px, splits=None, act=None, ident_rows=None, gmap_rows=None, div=None):
    ident = pd.DataFrame(ident_rows)
    gmap = pd.DataFrame(gmap_rows)
    act = pd.DataFrame(act or [], columns=["gvkey", "iid", "datadate", "actioncd", "actiontxt", "oldconm", "newconm", "gvkeyaqc"])
    act["datadate"] = pd.to_datetime(act.datadate)
    spl = pd.DataFrame(splits or [], columns=["tradingitemid", "exdate", "rate", "splittypeid", "appliedflag"])
    spl["exdate"] = pd.to_datetime(spl.exdate)
    dv = pd.DataFrame(div or [], columns=["tradingitemid", "exdate", "paydate", "divamount", "dividendtypeid", "currencyid"])
    dv["exdate"] = pd.to_datetime(dv.exdate)
    return {"ident": ident, "gmap": gmap, "act": act, "split": spl, "div": dv, "px": px}


def _ident(tid, sid, cid, name, sym="X", exch=104, secname="Common Stock", subtype=1, status=15):
    return dict(tradingitemid=tid, securityid=sid, tickersymbol=sym, exchangeid=exch, currencyid=160, tradingitemstatusid=status,
                companyid=cid, securityname=secname, securitystartdate=pd.NaT, securityenddate=pd.NaT, securitysubtypeid=subtype, companyname=name)


def _lc(ids, last="2020-01-02"):
    """old ids (odd) end on `last` (the events endDate of the fixture); new ids (even) are still trading."""
    return pd.DataFrame({"tradingItemId": ids, "firstTradeDate": pd.Timestamp("2010-01-04"),
                         "lastTradeDate": [pd.Timestamp(last) if i % 2 == 1 else pd.Timestamp("2027-01-04") for i in ids]})


def test_classify_reverse_split_at_transition_and_inside_series():
    # pair A: 1-for-10 reverse split whose ciqsplit exdate is the new id's first close -> ratio 0.1, class simple_rename
    # pair B: same text but the split is already inside the old id's series (adjf step on its last day) -> ratio 1.0
    end = pd.Timestamp("2015-06-30")
    pxA = pd.concat([_px(1, ["2015-06-26", "2015-06-29", "2015-06-30"], [1.0, 1.05, 1.0], adjf=0.1),
                     _px(2, ["2015-07-01", "2015-07-02"], [10.5, 10.4], adjf=1.0)])
    pxB = pd.concat([_px(3, ["2015-06-26", "2015-06-29", "2015-06-30"], [1.0, 1.05, 10.2], adjf=[0.1, 0.1, 1.0]),
                     _px(4, ["2015-07-01", "2015-07-02"], [10.5, 10.4], adjf=1.0)])
    ev = _evidence(pd.concat([pxA, pxB]),
                   splits=[dict(tradingitemid=1, exdate="2015-07-01", rate=0.1, splittypeid=12, appliedflag=1),
                           dict(tradingitemid=3, exdate="2015-06-30", rate=0.1, splittypeid=12, appliedflag=1)],
                   act=[dict(gvkey="1", iid="01", datadate="2015-07-01", actioncd="N/C", actiontxt="1-FOR-10 REVERSE STOCK SPLIT; NAME AND CUSIP CHANGED", oldconm="OLD A", newconm="NEW A", gvkeyaqc=None),
                        dict(gvkey="2", iid="01", datadate="2015-07-01", actioncd="N/C", actiontxt="1-FOR-10 REVERSE STOCK SPLIT; NAME AND CUSIP CHANGED", oldconm="OLD B", newconm="NEW B", gvkeyaqc=None)],
                   ident_rows=[_ident(1, 10, 100, "Old A Inc"), _ident(2, 11, 101, "New A Inc"), _ident(3, 30, 300, "Old B Inc"), _ident(4, 31, 301, "New B Inc")],
                   gmap_rows=[dict(objectid=1, gvkey="1", iid="01"), dict(objectid=2, gvkey="1", iid="01"), dict(objectid=3, gvkey="2", iid="01"), dict(objectid=4, gvkey="2", iid="01")])
    rej = pd.DataFrame({"tid": [1, 3], "succ": [2, 4], "end": [end, end], "R": [9.5, 0.03], "sf_ratio": [1.0, 1.0]})
    out = mg.classify_rename_rejections(rej, ev, _lc([1, 2, 3, 4], "2015-06-30"))
    a, b = out.set_index("oldId").loc[1], out.set_index("oldId").loc[3]
    assert a.linkClass == "simple_rename" and a.ratioSource == "ciqsplit" and abs(a.ratio - 0.1) < 1e-12 and abs(a.R_adopted - 0.05) < 1e-9
    assert b.ratio == 1.0 and b.ratioSource == "co_acthist_text" and "inside a series" in b.evidence and abs(b.R_adopted - (10.5 / 10.2 - 1)) < 1e-9


def test_classify_de_spac_mismatch_merger_spinoff():
    end = pd.Timestamp("2022-03-21")
    common = ["2022-03-16", "2022-03-17", "2022-03-18", "2022-03-21"]
    px = pd.concat([_px(1, common, [9.9, 9.95, 10.0, 9.7]), _px(2, common + ["2022-03-22"], [9.9, 9.95, 10.0, 9.7, 15.5]),   # stitched copy, +60% de-SPAC day
                    _px(3, common, [5.0, 5.1, 5.0, 5.2]), _px(4, common + ["2022-03-22"], [50.0, 51.0, 52.0, 51.0, 50.0]),   # two lines, no N/C row -> mismatch
                    _px(5, common, [40.0, 40.5, 41.0, 41.2]), _px(6, common + ["2022-03-22"], [30.0, 30.2, 30.5, 30.4, 31.0]),  # merger, terms in text
                    _px(7, common, [20.0, 20.5, 21.0, 21.2]), _px(8, ["2022-03-22"], [14.0])])                                  # reverse spin-off
    ev = _evidence(px,
                   act=[dict(gvkey="A", iid="01", datadate="2022-03-22", actioncd="N/C", actiontxt=None, oldconm="MOTIVE CAPITAL CORP -REDH", newconm="FORGE GLOBAL HLDGS", gvkeyaqc=None),
                        dict(gvkey="C", iid="01", datadate="2022-03-22", actioncd="N/C", actiontxt="TERMS: 1.325 SHARES OF NEWCO FOR EVERY SHARE OF OLDCO HELD.", oldconm="OLDCO", newconm="NEWCO", gvkeyaqc=None),
                        dict(gvkey="D", iid="01", datadate="2022-03-22", actioncd="N/C", actiontxt=None, oldconm="PARENT HOLDINGS", newconm="PARENT CO", gvkeyaqc=None),
                        dict(gvkey="D", iid="01", datadate="2022-03-22", actioncd="SPN", actiontxt="PARENT CO SPUN-OFF CHILD CO IN A REVERSE SPINOFF. HOLDERS RECEIVED 1 SHARE OF CHILD", oldconm="PARENT HOLDINGS", newconm="PARENT CO", gvkeyaqc="Z")],
                   ident_rows=[_ident(1, 10, 100, "Motive Capital Corp."), _ident(2, 11, 101, "Forge Global Holdings, Inc."),
                               _ident(3, 30, 300, "Furr's Restaurant Group"), _ident(4, 31, 301, "Fronteer Gold Inc."),
                               _ident(5, 50, 500, "Oldco Inc"), _ident(6, 51, 501, "Newco Inc"),
                               _ident(7, 70, 700, "Parent Holdings"), _ident(8, 71, 700, "Parent Co", secname="New Common Stock")],
                   gmap_rows=[dict(objectid=i, gvkey=g, iid="01") for i, g in [(1, "A"), (2, "A"), (3, "B"), (4, "B"), (5, "C"), (6, "C"), (7, "D"), (8, "D")]])
    rej = pd.DataFrame({"tid": [1, 3, 5, 7], "succ": [2, 4, 6, 8], "end": [end] * 4, "R": [0.6, 8.6, -0.25, -0.34], "sf_ratio": [1.0] * 4})
    out = mg.classify_rename_rejections(rej, ev, _lc(list(range(1, 9)), "2022-03-21")).set_index("oldId")
    assert out.loc[1].linkClass == "de_spac" and out.loc[1].ratio == 1.0 and out.loc[1].confidence == "medium" and out.loc[1].stitchedCopy
    assert out.loc[3].linkClass == "mismatch" and np.isnan(out.loc[3].ratio) and out.loc[3].relation == "unknown"
    assert out.loc[5].linkClass == "merger" and abs(out.loc[5].ratio - 1.325) < 1e-12 and out.loc[5].ratioSource == "co_acthist_text"
    assert out.loc[7].linkClass == "spin_off" and out.loc[7].ratio == 1.0 and out.loc[7].relation == "rename"


def test_collect_legs_multi_leg_and_partial():
    """F1: a rename with a reverse split (0.1) plus a special cash dividend recorded on the old id with the new-basis amount and a
    child stock leg -> every leg is collected, the cash unit is resolved by total-consideration consistency, R_total is the
    all-legs return; an unpriced child makes the row partial."""
    old_last, new_first = pd.Timestamp("2023-10-19"), pd.Timestamp("2023-10-20")
    dv = pd.DataFrame({"tradingitemid": [1, 1, 1], "exdate": pd.to_datetime(["2023-10-20", "2023-10-20", "2023-06-01"]), "paydate": pd.NaT,
                       "divamount": [15.118, 0.5, 0.2], "dividendtypeid": [4, 20, 1], "currencyid": 160})   # 15.118 = new basis (x0.1 -> 1.5118)
    childpx = pd.DataFrame({"tradingitemid": [9, 9], "pricingdate": pd.to_datetime(["2023-10-20", "2023-10-23"]), "raw_close": [4.0, 4.1]})
    legs = mg.collect_legs(1, 2, 0.1, old_last, new_first, 2.72, 10.9, dv, None, childpx, {}, {"child": 9, "childRatio": 0.25})
    assert abs(legs["cash"] - 1.5118) < 1e-9 and "new_basis_x_ratio" in legs["cashSource"]
    assert len(legs["stock"]) == 1 and abs(legs["stock"][0]["value"] - 1.0) < 1e-9 and not legs["partial"]
    assert abs(legs["R_total"] - ((0.1 * 10.9 + 1.5118 + 1.0) / 2.72 - 1)) < 1e-12 and legs["flag"].startswith("multi_leg")
    legs2 = mg.collect_legs(1, 2, 0.1, old_last, new_first, 2.72, 10.9, dv, None, childpx.iloc[0:0], {}, {"child": 9, "childRatio": 0.25})
    assert legs2["partial"] and "no close at transition" in legs2["partialNote"] and np.isnan(legs2["R_total"])
    legs3 = mg.collect_legs(1, 2, np.nan, old_last, new_first, 2.72, 10.9, dv, None, childpx, {}, {})   # unresolved ratio: no partial flag
    assert not legs3["partial"] and np.isnan(legs3["R_total"])


def test_merge_base_data_keeps_global_sort(tmp_path=None):
    """F9: aux rows are placed inside the base row group whose id range covers them (or appended after the last id) so the merged
    file stays globally sorted by (tradingItemId, date)."""
    import tempfile
    import pyarrow as pa
    import pyarrow.parquet as pq

    d = Path(tempfile.mkdtemp(prefix="r3merge_"))
    schema = pa.schema([("date", pa.timestamp("ns")), ("tradingItemId", pa.int64()), ("close", pa.float32())])
    dates = pd.to_datetime(["2020-01-02", "2020-01-03"])
    def rows(ids):
        return pd.DataFrame({"date": np.tile(dates.values, len(ids)), "tradingItemId": np.repeat(ids, 2), "close": 1.0}).astype({"close": "float32"})
    base = d / "base.parquet"
    w = pq.ParquetWriter(base, schema)
    for grp in ([10, 20, 30], [40, 50]):        # two id-chunk row groups
        w.write_table(pa.Table.from_pandas(rows(grp), schema=schema, preserve_index=False))
    w.close()
    aux = d / "aux.parquet"
    pq.write_table(pa.Table.from_pandas(rows([25, 45, 60]), schema=schema, preserve_index=False), aux)     # inside g1, inside g2, after
    info = ma.merge_base_data(base, aux, d / "merged.parquet")
    m = pq.read_table(d / "merged.parquet").to_pandas()
    assert info["aux_rows_placed"] == 6 and len(m) == 16 and info["row_groups"] == 3
    key = list(zip(m.tradingItemId, m.date))
    assert key == sorted(key)
    pf = pq.ParquetFile(d / "merged.parquet")
    maxes = [pf.read_row_group(i, columns=["tradingItemId"]).column(0).to_numpy().max() for i in range(pf.num_row_groups)]
    assert maxes == sorted(maxes)


# --------------------------------------------------------------------------------------
# lineage v2 assembly + gates
# --------------------------------------------------------------------------------------
def _rc1_lineage():
    return pd.DataFrame({"oldId": [1, 3, 5], "newId": [2, 4, 6], "relation": ["rename", "rename", "merger"],
                         "effectiveDate": pd.to_datetime(["2015-07-01", "2015-07-01", "2016-01-04"]), "ratio": [np.nan, np.nan, 0.5],
                         "evidence": ["e1", "e3", "e5"], "confidence": ["high", "high", "high"]})


def test_build_lineage_v2_updates_and_appends():
    reclass = pd.DataFrame([dict(oldId=1, newId=2, relation="rename", ratio=0.1, confidence="high", evidence="split", ratioSource="ciqsplit",
                                 linkClass="simple_rename", reviewFlag="", effectiveDate=pd.Timestamp("2015-07-01")),
                            dict(oldId=3, newId=4, relation="unknown", ratio=np.nan, confidence="low", evidence="mm", ratioSource="none",
                                 linkClass="mismatch", reviewFlag="mismatch", effectiveDate=pd.Timestamp("2015-07-01"))])
    overrides = [dict(oldId=9, newId=10, relation="relist", ratio=1.0, confidence="high", ratioSource="security_terms", evidence="adr", effectiveDate="2026-09-14")]
    v2 = mg.build_lineage_v2(_rc1_lineage(), reclass, overrides)
    assert list(v2.columns) == mg.LINEAGE_V2_COLS
    assert len(v2) == 4 and not v2.duplicated(["oldId", "newId"]).any()
    r = v2.set_index(["oldId", "newId"])
    assert r.loc[(1, 2)].ratio == 0.1 and r.loc[(1, 2)].version == mg.LINEAGE_V2_VERSION and r.loc[(1, 2)].linkClass == "simple_rename"
    assert r.loc[(3, 4)].relation == "unknown" and np.isnan(r.loc[(3, 4)].ratio)
    assert r.loc[(5, 6)].version == "v1-rc1" and r.loc[(5, 6)].ratio == 0.5
    assert r.loc[(9, 10)].relation == "relist" and r.loc[(9, 10)].effectiveDate == pd.Timestamp("2026-09-14")
    with pytest.raises(RuntimeError):
        mg.build_lineage_v2(pd.concat([_rc1_lineage(), _rc1_lineage().head(1)]), reclass.iloc[0:0], [])


def test_lineage_v2_gates_self_test_and_planted_overlap():
    st = mq.lineage_v2_self_test(CAL)
    assert st["pass"] and st["planted_pairs"] == 1 and st["clean_pairs"] == 0
    lin = pd.DataFrame({"oldId": [1, 3], "newId": [2, 4], "relation": ["rename", "relist"], "confidence": ["high", "medium"], "ratio": [1.0, 1.0],
                        "effectiveDate": pd.to_datetime(["2015-01-05", "2016-01-05"]), "evidence": ["", ""], "ratioSource": ["a", "b"],
                        "linkClass": ["simple_rename", "relist"], "reviewFlag": ["", ""], "sources": ["", ""], "version": ["v2-x", "v2-x"]})
    lc = pd.DataFrame({"tradingItemId": [1, 2, 3, 4], "firstTradeDate": pd.to_datetime(["2010-01-04", "2015-01-05", "2010-01-04", "2015-06-01"]),
                       "lastTradeDate": pd.to_datetime(["2015-01-02", "2020-01-02", "2016-01-04", "2020-01-02"])})
    res = mq.lineage_v2_gates(lin, lc, CAL)
    assert res["contract"]["pass"] and res["dual_tradable"]["pairs"] == 1 and res["dual_tradable"]["top"][0]["oldId"] == 3
    # exclusions covering the overlap window remove it from the uncovered count
    ex = pd.DataFrame({"tradingItemId": [4], "fromDate": [pd.Timestamp("2015-06-01")], "toDate": [pd.Timestamp("2016-01-04")]})
    res2 = mq.lineage_v2_gates(lin, lc, CAL, ex)
    assert res2["dual_tradable"]["pairs_not_covered_by_exclusions"] == 0
    bad = lin.copy()
    bad.loc[0, "relation"] = "bogus"
    assert not mq.lineage_v2_gates(bad, lc, CAL)["contract"]["pass"]


# --------------------------------------------------------------------------------------
# master_aux: collect / aux_instruments / aux lifecycle / coverage gate / lifecycle_dates
# --------------------------------------------------------------------------------------
def test_collect_references_and_aux_instruments():
    inst = pd.DataFrame({"tradingItemId": [1, 2, 3]})
    events = pd.DataFrame({"tradingItemId": [1, 2], "successorTradingItemId": [100, 2], "endDate": pd.to_datetime(["2010-05-03", "2011-01-03"])})
    lineage = pd.DataFrame({"oldId": [2, 200], "newId": [100, 3], "effectiveDate": pd.to_datetime(["2011-01-04", "2026-09-14"])})
    ledger = pd.DataFrame({"tradingItemId": [3, 3], "childTradingItemId": [300, 1], "exDate": pd.to_datetime(["2012-02-01", "2012-02-01"])})
    dist = pd.DataFrame({"tradingItemId": [3], "exDate": pd.to_datetime(["2012-02-01"]), "kind": ["stock"], "newTradingItemId": [300]})
    refs = ma.collect_references(inst, events, lineage, ledger, dist)
    assert set(refs.tradingItemId) == {100, 200, 300}            # ids inside the set (2, 1, 3) are never aux
    assert set(refs[refs.tradingItemId == 200].reason) == {"lineage_predecessor"}
    ident = pd.DataFrame({"tradingItemId": [100, 200, 300], "symbol": ["A", "B", None], "companyName": ["A co", "B co", None],
                          "exchangeId": [106, 106, None], "tiCurrencyId": [160, 50, None], "securitySubTypeId": [1, 2, None],
                          "tradingItemStatusId": [15, 8, None], "securityStartDate": pd.NaT, "securityEndDate": pd.NaT})
    aux = ma.build_aux_instruments(refs, ident)
    assert list(aux.columns) == ma.AUX_COLS and len(aux) == 3
    a = aux.set_index("tradingItemId")
    assert a.loc[100].reason == "events_successor+lineage_successor" and a.loc[100].nReferences == 2 and a.loc[100].priceFetch == "usd"
    assert a.loc[100].firstNeededDate == pd.Timestamp("2010-05-03") - pd.Timedelta(days=ma.NEEDED_LOOKBACK_DAYS)
    assert a.loc[200].priceFetch == "skipped_non_usd" and a.loc[300].priceFetch == "skipped_no_identity"
    assert a.loc[300].reason == "distribution_child+ledger_child"


def test_aux_lifecycle_and_coverage_gate():
    aux = pd.DataFrame({"tradingItemId": [100, 200, 300], "reason": ["events_successor", "lineage_predecessor", "ledger_child"],
                        "referencedBy": ["", "", ""], "firstNeededDate": pd.to_datetime(["2010-03-19", "2026-07-31", "2012-01-01"]), "nReferences": 1,
                        "symbol": ["A", "B", "C"], "companyName": ["A", "B", "C"], "exchangeId": pd.array([106, 106, 134], dtype="Int64"),
                        "tiCurrencyId": pd.array([160, 160, 160], dtype="Int64"), "securitySubTypeId": pd.array([1, 2, 1], dtype="Int64"),
                        "tradingItemStatusId": pd.array([15, 8, 11], dtype="Int64"), "securityStartDate": pd.NaT,
                        "securityEndDate": pd.to_datetime([None, "2026-09-11", "2013-01-01"]), "priceFetch": "usd"})
    cov = pd.DataFrame({"tradingItemId": [100, 200, 300], "firstCloseDate": pd.to_datetime(["2000-01-03", "2000-01-03", None]),
                        "lastCloseDate": pd.to_datetime(["2026-09-15", "2026-09-11", None]), "nCloseRows": [6000, 6700, 0], "nRows": [6000, 6713, 0]})
    lc = ma.aux_lifecycle(aux, cov, pd.Timestamp("2026-09-15"), pd.Timestamp("2026-09-17"))
    assert (lc.status == "aux").all() and list(lc.columns) == ma.ml.LIFECYCLE_COLS
    k = lc.set_index("tradingItemId")
    assert k.loc[100].endReason == "active" and k.loc[200].endReason == "merger" and k.loc[300].endReason == "data_gap"
    rep = ma.aux_coverage(aux, cov, CAL)
    r = rep.set_index("tradingItemId")
    assert r.loc[100].coverage == "ok" and r.loc[200].coverage == "ok" and r.loc[300].coverage == "no_close_rows"
    inst = pd.DataFrame({"tradingItemId": [1, 2]})
    lineage = pd.DataFrame({"oldId": [1, 200], "newId": [100, 2]})
    events = pd.DataFrame({"successorTradingItemId": pd.array([100, None], dtype="Int64")})
    ledger = pd.DataFrame({"childTradingItemId": pd.array([300], dtype="Int64")})
    res = mq.aux_coverage_check(inst, aux, lineage, events, ledger, {100, 200}, lc)
    assert res["pass"] is True and res["aux_usd_without_prices"] == 1         # 300 has no ciq rows: reported, not a consistency failure
    res2 = mq.aux_coverage_check(inst, aux, lineage, events, ledger, {100, 200, 300}, lc)
    assert res2["pass"] is True
    res3 = mq.aux_coverage_check(inst, aux, lineage, events, pd.DataFrame({"childTradingItemId": pd.array([999], dtype="Int64")}), {100, 200, 300}, lc)
    assert res3["pass"] is False and res3["ledger_children_uncovered"] == [999]


def test_lifecycle_dates_keeps_three_dates_separate():
    lc = pd.DataFrame({"tradingItemId": [1, 2, 3], "lastTradeDate": pd.to_datetime(["2019-12-04", "2018-04-03", "2020-06-30"]),
                       "lastTradeSource": ["ciq", "mi", "ciq"], "securityEndDate": pd.to_datetime(["2019-12-04", "2018-04-03", None]),
                       "ciqLastCloseDate": pd.to_datetime(["2008-08-25", "2018-04-03", "2020-06-30"]), "miLastCloseDate": pd.to_datetime([None, "2018-04-03", "2020-06-30"])})
    ev = pd.DataFrame({"tradingItemId": [1, 2, 3], "endDate": pd.to_datetime(["2019-12-04", "2018-04-03", "2020-06-29"]),
                       "endDateOriginal": pd.to_datetime(["2019-12-04", "2018-04-03", "2020-06-29"]), "endDateSource": ["db", "local", "db+capped_estimate"],
                       "settlementDate": pd.to_datetime(["2019-12-04", "2018-04-05", "2020-07-01"]), "actionCode": [None, "ACQ", "ACQ"],
                       "matchMethod": ["none", "gvkeyiid", "gvkeyiid"]})
    ledger = pd.DataFrame({"tradingItemId": [3], "exDate": pd.to_datetime(["2020-06-30"]), "eventType": ["merger_cash"], "payDate": pd.to_datetime(["2020-07-06"])})
    d = ma.build_lifecycle_dates(lc, ev, ledger, {1})
    assert list(d.columns) == ma.DATES_COLS and len(d) == 3
    k = d.set_index("tradingItemId")
    # id 1: DB end date 4,118 days after the last close -> effectiveDate kept (source ciqsecurity), lastTradeDate untouched
    assert k.loc[1].mismatchClass == "match" and k.loc[1].effectiveDateSource == "ciqsecurity.securityenddate" and pd.isna(k.loc[1].settlementDate)
    assert k.loc[1].lastTradeDate == pd.Timestamp("2019-12-04") and bool(k.loc[1].everTop500)
    # id 2: local end date = no independent effective date; settlement from co_acthist
    assert "no independent date" in k.loc[2].effectiveDateSource and k.loc[2].settlementDate == pd.Timestamp("2018-04-05") and k.loc[2].gapSettlementMinusLastTrade == 2
    # id 3: endDate before the last close; pay date from the ledger
    assert k.loc[3].mismatchClass == "endDate_before_lastTrade" and k.loc[3].payDate == pd.Timestamp("2020-07-06") and k.loc[3].payDateSource == "ca_ledger.payDate"


# --------------------------------------------------------------------------------------
# optional: gates on the real R3 stage (R3_STAGE env var)
# --------------------------------------------------------------------------------------
@pytest.mark.skipif(not os.environ.get("R3_STAGE"), reason="set R3_STAGE to run on the stage files")
def test_real_stage_gates():
    stage = Path(os.environ["R3_STAGE"])
    lin = pd.read_parquet(stage / "master" / "lineage.parquet")
    lc_path = stage / "master" / "lifecycle.parquet"          # R3-patched copy when present, else the rc1 input
    if not lc_path.exists():
        lc_path = stage / "in" / "master" / "lifecycle.parquet"
    lc = pd.concat([pd.read_parquet(lc_path), pd.read_parquet(stage / "master" / "lifecycle_aux.parquet")])
    cal = pd.read_parquet(stage / "in" / "calendar.parquet").date.to_numpy(dtype="datetime64[ns]")
    ex = pd.read_parquet(stage / "in" / "master" / "tradable_exclusions.parquet")
    res = mq.lineage_v2_gates(lin, lc, cal, ex, stage / "qa")
    assert res["self_test"]["pass"] and res["contract"]["pass"]
    inst = pd.read_parquet(stage / "in" / "master" / "instruments.parquet")
    aux = pd.read_parquet(stage / "master" / "aux_instruments.parquet")
    ev = pd.read_parquet(stage / "in" / "events" / "corporate_actions.parquet", columns=["successorTradingItemId"])
    base_ids = set(pd.read_parquet(stage / "base_data_aux.parquet", columns=["tradingItemId"]).tradingItemId.unique().tolist())
    lca = pd.read_parquet(stage / "master" / "lifecycle_aux.parquet")
    led_path = stage.parent / "R1" / "events" / "ca_ledger.parquet"
    led = pd.read_parquet(led_path, columns=["childTradingItemId"]) if led_path.exists() else None
    res2 = mq.aux_coverage_check(inst, aux, lin, ev, led, base_ids, lca, stage / "qa")
    assert res2["aux_in_symbol_set"] == 0 and res2["n_lineage_ids_uncovered"] == 0 and res2["n_events_successors_uncovered"] == 0


# --------------------------------------------------------------------------------------
# self-runner (no pytest needed)
# --------------------------------------------------------------------------------------
def main(argv=None) -> int:
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    rows = []
    for t in tests:
        if getattr(t, "_skip", False):
            rows.append((t.__name__, "SKIP", ""))
            continue
        try:
            if hasattr(t, "_cases"):
                names, cases = t._cases
                keys = [n.strip() for n in names.split(",")]
                for case in cases:
                    t(**dict(zip(keys, case)))
            else:
                t()
            rows.append((t.__name__, "PASS", ""))
        except Exception as e:  # noqa: BLE001
            rows.append((t.__name__, "FAIL", f"{type(e).__name__}: {e}"))
            traceback.print_exc()
    w = max(len(r[0]) for r in rows)
    for name, res, note in rows:
        print(f"{name:{w}s}  {res:5s}  {note}")
    n_fail = sum(r[1] == "FAIL" for r in rows)
    print(f"\n{len(rows) - n_fail}/{len(rows)} passed")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
