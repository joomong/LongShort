#!/home/jgo/usr/venv-3.11/bin/python
"""cache_v21/universe_qa.py - gates for the T5 universe tables (BRIEF §1 numeric-report rules).

Inputs: runs written by universe_build.py under `T5_work/runs/<name>/` (membership tables + run.json), the
prototype masks `TopUniverseV2_v1rule/*.parquet`, `TopUniverseV2/*.parquet` (wide bool, int-like str columns),
the v1 snapshot `TopUniverseV1/*.parquet`, the trading calendar written by universe_build, T1 master.

Outputs (T5/qa/):
  universe_gates.md               judgement table; overlap tables (|A∩B|/|A| median, p05, min; Jaccard) 2010+ at id
                                  level and at line level (old ids OR-ed into their lineage terminal id); member counts;
                                  ablation ladder; per-item cause table; double-counting gate; counterexample self-test
  universe_overlap_summary.csv, universe_overlap_daily.parquet
  universe_diff_items.csv         items with differing name-days vs the prototype v1rule masks, day counts per cause
  tradable_effect.md (+ tradable_effect_items.csv)   full / strict_curprim / line_curprim vs notrad
  universe_sensitivity.csv        dollar-volume estimator variants
  universe_duplicate_gate.csv     lineage double-counting gate per run

CLI:
  universe_qa.py all [--run full] [--notrad notrad] [--ladder strict_curprim,notrad,notrad_sadj,notrad_sadj_pmcap]
                     [--counter notrad_entry30,notrad_dv1e6] [--extra full_sadj,full_product] [--others strict_curprim,line_curprim]
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from universe_common import (  # noqa: E402
    PROTO_ROOT, T1_MASTER, T2_ROOT, T5_ROOT, T5_WORK, UNIVERSE_NAMES, V1_SNAPSHOT, Timer, load_current,
    load_exclusions, load_instruments, load_lifecycle, log, md_table, read_json, setup_logging, to_ns_i8, write_json,
)
from universe_build import (  # noqa: E402
    UNIV_PARAMS, intervals_to_mask, line_groups, lineage_duplicates, lineage_edges, stitched_flags, stitched_old_map,
)
from loader import CacheV21  # noqa: E402

SINCE = pd.Timestamp("2010-01-01")
GATE_UNIVERSES = ("Top500", "Top1000", "ALL")
CAUSES = ["reattribution_old_new_id", "transition_gap_sticky_restart", "stitched_old_not_in_panel", "stitched_old_not_member",
          "tail_cut", "exclusion", "pit_predecessor_added", "new_id", "line_state_effect", "rank_boundary_substitution",
          "dollar_volume_basis", "mcap_diff", "other"]


class Axes:
    """Panel axes (trading calendar rows, ids) + per-item attributes shared by all gates."""

    def __init__(self, t2_root: Path, master_root: Path, work: Path = T5_WORK):
        cv = CacheV21(t2_root, master_root=master_root, threads=8, validate=False)
        all_dates = cv.dates()
        cal = pd.read_parquet(work / "calendar.parquet")          # written by universe_build (trading-calendar rule)
        if not np.array_equal(pd.DatetimeIndex(cal.date).values, all_dates.values):
            raise RuntimeError("calendar.parquet does not match the base_data dates; rerun universe_build")
        self.keep_rows = cal.keep.to_numpy(dtype=bool)
        self.dates = all_dates[self.keep_rows]
        self.dates_i8 = self.dates.values.astype("datetime64[ns]").astype(np.int64)
        self.ids = cv.ids()
        self.id_index = pd.Index(self.ids)
        self.T, self.N = self.dates.size, self.ids.size
        self.sel2010 = pd.Series(self.dates >= SINCE)
        cur = load_current(master_root).set_index("tradingItemId").reindex(self.ids)
        inst = load_instruments(master_root).set_index("tradingItemId").reindex(self.ids)
        lc = load_lifecycle(master_root).set_index("tradingItemId").reindex(self.ids)
        self.symbol = cur.symbol.fillna("").to_numpy()
        self.name = inst.companyName.fillna("").to_numpy()
        self.in_proto = inst.inProtoV2.to_numpy(dtype=bool)
        self.in_v1rule = inst.inV1Rule.to_numpy(dtype=bool)
        self.exchange = cur.exchangeId.to_numpy()
        self.current_primary = cur.currentPrimary.to_numpy(dtype=bool)
        self.first_trade = lc.firstTradeDate.values.astype("datetime64[ns]")
        self.last_trade = lc.lastTradeDate.values.astype("datetime64[ns]")
        self.lo_trade = np.searchsorted(self.dates_i8, self.first_trade.astype(np.int64), side="left")
        self.hi_trade = np.searchsorted(self.dates_i8, self.last_trade.astype(np.int64), side="right")
        self.stitched = stitched_flags(lc)
        lineage = pq.read_table(master_root / "lineage.parquet").to_pandas()
        self.old_of = stitched_old_map(lc, lineage, self.ids, self.stitched)
        self.edges = lineage_edges(lineage, self.ids)
        self.line_col = line_groups(self.edges, self.ids)
        self.excluded = np.zeros(self.N, dtype=bool)
        self.excl_windows: dict[int, list[tuple[int, int]]] = {}
        excl = load_exclusions(master_root)
        if excl is not None:
            for r in excl.itertuples(index=False):
                if r.tradingItemId in self.id_index:
                    c = self.id_index.get_loc(r.tradingItemId)
                    self.excluded[c] = True
                    s = np.searchsorted(self.dates_i8, pd.Timestamp(r.fromDate).value, side="left")
                    e = np.searchsorted(self.dates_i8, pd.Timestamp(r.toDate).value, side="right")
                    self.excl_windows.setdefault(c, []).append((s, e))

    def to_line(self, mask: np.ndarray) -> np.ndarray:
        """Collapse a (T,N) id mask onto line columns (old ids OR-ed into their lineage terminal id column)."""
        out = np.zeros_like(mask)
        moved = self.line_col != np.arange(self.N)
        out[:, ~moved] = mask[:, ~moved]
        for c in np.flatnonzero(moved):
            out[:, self.line_col[c]] |= mask[:, c]
        return out


def load_run_masks(run_dir: Path, variant: str, axes: Axes, universes=UNIVERSE_NAMES) -> dict[str, np.ndarray]:
    fname = "membership.parquet" if variant == "all" else "membership_v1rule.parquet"
    df = pq.read_table(run_dir / fname).to_pandas()
    return {u: intervals_to_mask(df[df.universe == u], axes.dates_i8, axes.ids) for u in universes}


def load_wide_bool(path: Path, axes: Axes) -> tuple[np.ndarray, np.ndarray, int]:
    """Wide bool parquet -> (mask aligned to axes (T x N), row_valid bool (T) for dates present, member-days outside)."""
    t = pq.read_table(path)
    pdates = to_ns_i8(t.column("__index_level_0__").to_numpy())
    r = np.searchsorted(axes.dates_i8, pdates)
    okr = (r < axes.T)
    okr &= axes.dates_i8[np.minimum(r, axes.T - 1)] == pdates
    out = np.zeros((axes.T, axes.N), dtype=bool)
    row_valid = np.zeros(axes.T, dtype=bool)
    row_valid[r[okr]] = True
    outside = 0
    for name in t.column_names:
        if name == "__index_level_0__":
            continue
        tid = int(name)
        v = t.column(name).to_numpy(zero_copy_only=False).astype(bool)
        if tid in axes.id_index:
            out[r[okr], axes.id_index.get_loc(tid)] = v[okr]
        else:
            outside += int(v[okr].sum())          # member-days of items our panel does not carry
    return out, row_valid, outside


def overlap_stats(a: np.ndarray, b: np.ndarray, rows: np.ndarray, dates: pd.DatetimeIndex, label: str,
                  universe: str, outside_b_days: int = 0) -> tuple[dict, pd.DataFrame]:
    """a = ours, b = reference; rows = bool (T) of dates to use.  Fractions are per day, then median/p05/min."""
    A, B = a[rows], b[rows]
    na = A.sum(axis=1).astype(np.int64)
    nb = B.sum(axis=1).astype(np.int64)
    ni = (A & B).sum(axis=1).astype(np.int64)
    ok = (na > 0) & (nb > 0)
    f_a, f_b = ni[ok] / na[ok], ni[ok] / nb[ok]
    jac = ni[ok] / (na[ok] + nb[ok] - ni[ok])
    stats = {
        "comparison": label, "universe": universe, "days": int(ok.sum()),
        "ours_med": float(np.median(na[ok])), "ref_med": float(np.median(nb[ok])),
        "ov_of_ours_med": float(np.median(f_a)), "ov_of_ours_p05": float(np.quantile(f_a, 0.05)), "ov_of_ours_min": float(f_a.min()),
        "ov_of_ref_med": float(np.median(f_b)), "ov_of_ref_p05": float(np.quantile(f_b, 0.05)), "ov_of_ref_min": float(f_b.min()),
        "jaccard_med": float(np.median(jac)), "jaccard_pooled": float(ni.sum() / max((na.sum() + nb.sum() - ni.sum()), 1)),
        "only_ours_name_days": int((A & ~B).sum()), "only_ref_name_days": int((B & ~A).sum()),
        "ref_name_days_outside_panel": int(outside_b_days),
    }
    daily = pd.DataFrame({"date": dates[rows], "comparison": label, "universe": universe, "n_ours": na, "n_ref": nb, "n_both": ni})
    return stats, daily


# ------------------------------------------------------------------------------------------------------
def gate_overlaps(axes: Axes, ours_v1rule: dict, ours_all: dict, out_dir: Path) -> pd.DataFrame:
    rows, dailies = [], []
    refs = [("proto_v1rule", PROTO_ROOT / "TopUniverseV2_v1rule", ours_v1rule),
            ("proto_all", PROTO_ROOT / "TopUniverseV2", ours_all),
            ("v1_snapshot(v1rule)", V1_SNAPSHOT / "TopUniverseV1", ours_v1rule),
            ("v1_snapshot(all)", V1_SNAPSHOT / "TopUniverseV1", ours_all)]
    for label, root, ours in refs:
        for u in UNIVERSE_NAMES:
            p = root / f"{u}.parquet"
            if not p.exists():
                continue
            ref, row_valid, outside = load_wide_bool(p, axes)
            use = row_valid & axes.sel2010.to_numpy()
            for suffix, fa, fb in (("", ours[u], ref), ("[line]", axes.to_line(ours[u]), axes.to_line(ref))):
                if suffix and not label.startswith("proto"):
                    continue
                st, daily = overlap_stats(fa, fb, use, axes.dates, label + suffix, u, outside)
                st_full, _ = overlap_stats(fa, fb, row_valid, axes.dates, label + suffix, u, outside)
                st["ov_of_ours_med_2000plus"] = st_full["ov_of_ours_med"]
                st["days_2000plus"] = st_full["days"]
                rows.append(st)
                dailies.append(daily)
            del ref
        log.info("overlap %s done", label)
    tab = pd.DataFrame(rows)
    pd.concat(dailies, ignore_index=True).to_parquet(out_dir / "universe_overlap_daily.parquet", index=False)
    return tab


def ladder(axes: Axes, run_names: list[str], work: Path) -> tuple[pd.DataFrame, dict, dict]:
    """Ablation ladder (v1rule variant): each run vs the prototype v1rule masks and vs the previous run.

    Returns (table, per_item {(run, universe): symdiff days per column}, protos)."""
    protos = {u: load_wide_bool(PROTO_ROOT / "TopUniverseV2_v1rule" / f"{u}.parquet", axes) for u in GATE_UNIVERSES}
    rows, per_item, prev = [], {}, None
    for name in run_names:
        run_dir = work / "runs" / name
        info = read_json(run_dir / "run.json")
        masks = load_run_masks(run_dir, "v1rule", axes, GATE_UNIVERSES)
        rec = {"run": name, "tradable": info["tradable"], "primary": info.get("primary", "current"), "dollar": info["dollar_mode"],
               "mcap": info["mcap_source"], "params": {k: v for k, v in info["params"].items() if v != UNIV_PARAMS[k]}}
        for u in GATE_UNIVERSES:
            ref, row_valid, _ = protos[u]
            use = row_valid & axes.sel2010.to_numpy()
            st, _ = overlap_stats(masks[u], ref, use, axes.dates, name, u)
            st_l, _ = overlap_stats(axes.to_line(masks[u]), axes.to_line(ref), use, axes.dates, name, u)
            rec[f"{u}_ov_med"] = st["ov_of_ours_med"]
            rec[f"{u}_ov_p05"] = st["ov_of_ours_p05"]
            rec[f"{u}_symdiff"] = st["only_ours_name_days"] + st["only_ref_name_days"]
            rec[f"{u}_line_ov_med"] = st_l["ov_of_ours_med"]
            rec[f"{u}_line_symdiff"] = st_l["only_ours_name_days"] + st_l["only_ref_name_days"]
            rec[f"{u}_vs_prev"] = int((masks[u][use] ^ prev[u][use]).sum()) if prev is not None else 0
            per_item[(name, u)] = (masks[u][use] ^ ref[use]).sum(axis=0)
        rows.append(rec)
        prev = masks
    return pd.DataFrame(rows), per_item, protos


def classify_items(axes: Axes, work: Path, full_run: str, ladder_names: list[str], per_item: dict, protos: dict,
                   out_dir: Path) -> pd.DataFrame:
    """Per differing item (v1rule variant vs prototype v1rule, 2010+): day counts per cause.

    Causes (day-level, in priority order):
      reattribution_old_new_id      the line-level masks agree that day (prototype holds the stitched new id, T5 the
                                    old id that actually traded, or vice versa) - same company, different id
      transition_gap_sticky_restart prototype-only days of a lifted new id within StickyEntryDays+1 trading days after
                                    its firstTradeDate (the new id re-enters the sticky rule from zero)
      stitched_old_not_in_panel     prototype-only stitched days of a lifted id whose lineage old id is not in the panel
      stitched_old_not_member       prototype-only stitched days where the old id exists but is not a member
      tail_cut                      prototype-only days after the id's lastTradeDate
      exclusion                     prototype-only days inside a tradable_exclusions window
      pit_predecessor_added         T5-only days of a non-currentPrimary predecessor admitted by PIT primary (the
                                    prototype had no id of that line: successor not stitched or not eligible)
      new_id                        item not in the prototype symbol set
      rank_boundary_substitution    residual that disappears once lifecycle/PIT are switched off (names that enter or
                                    leave at the rank/threshold boundary because the candidate pool changed)
      dollar_volume_basis / mcap_diff   residual that disappears at that ladder step
      other                         remainder"""
    use = axes.sel2010.to_numpy()
    entry = int(UNIV_PARAMS["StickyEntryDays"]) + 1
    row_of_use = np.flatnonzero(use)
    rows = []
    ours_all = load_run_masks(work / "runs" / full_run, "v1rule", axes, GATE_UNIVERSES)
    step_cause = {"strict_pit": "line_state_effect", "notrad_sadj": "dollar_volume_basis", "notrad_sadj_pmcap": "mcap_diff"}
    for u in GATE_UNIVERSES:
        ref, row_valid, _ = protos[u]
        ours = ours_all[u]
        line_diff = (axes.to_line(ours) ^ axes.to_line(ref))[use]
        O, P = ours[use], ref[use]
        diff = O ^ P
        items = np.flatnonzero(diff.sum(axis=0) > 0)
        for c in items:
            d_rows = np.flatnonzero(diff[:, c])
            only_p = P[d_rows, c] & ~O[d_rows, c]
            abs_rows = row_of_use[d_rows]                     # rows in the full calendar
            counts = {k: 0 for k in CAUSES}
            remaining = np.ones(d_rows.size, dtype=bool)
            if not axes.in_proto[c]:
                counts["new_id"] = int(d_rows.size)
                remaining[:] = False
            agree = ~line_diff[d_rows, axes.line_col[c]]      # line level agrees -> id re-attribution within the line
            counts["reattribution_old_new_id"] += int((remaining & agree).sum())
            remaining &= ~agree
            if axes.stitched[c]:
                gap = only_p & (abs_rows >= axes.lo_trade[c]) & (abs_rows < axes.lo_trade[c] + entry)
                counts["transition_gap_sticky_restart"] += int((remaining & gap).sum())
                remaining &= ~gap
                pre = only_p & (abs_rows < axes.lo_trade[c])
                key = "stitched_old_not_in_panel" if axes.old_of[c] < 0 else "stitched_old_not_member"
                counts[key] += int((remaining & pre).sum())
                remaining &= ~pre
            tail = only_p & (abs_rows >= axes.hi_trade[c])
            counts["tail_cut"] += int((remaining & tail).sum())
            remaining &= ~tail
            if c in axes.excl_windows:
                exw = np.zeros(d_rows.size, dtype=bool)
                for s, e in axes.excl_windows[c]:
                    exw |= (abs_rows >= s) & (abs_rows < e)
                counts["exclusion"] += int((remaining & only_p & exw).sum())
                remaining &= ~(only_p & exw)
            if not axes.current_primary[c]:
                # a lineage predecessor admitted by PIT primary where the prototype had no id of the line (e.g. GGP
                # before BPYU: successor not stitched, or successor not eligible in the prototype)
                pa = remaining & ~only_p
                counts["pit_predecessor_added"] += int(pa.sum())
                remaining &= ~pa
            resid_cause = "other"
            for n in ladder_names[1:]:
                if per_item[(n, u)][c] == 0:
                    resid_cause = step_cause.get(n, "rank_boundary_substitution" if n in ("strict_curprim", "notrad") else "other")
                    break
            counts[resid_cause] += int(remaining.sum())
            dominant = max(counts, key=counts.get)
            rows.append({"universe": u, "tradingItemId": int(axes.ids[c]), "symbol": axes.symbol[c], "companyName": axes.name[c],
                         "exchangeId": axes.exchange[c], "inProto": bool(axes.in_proto[c]), "currentPrimary": bool(axes.current_primary[c]),
                         "stitched": bool(axes.stitched[c]), "firstTradeDate": pd.Timestamp(axes.first_trade[c]).date(),
                         "lastTradeDate": pd.Timestamp(axes.last_trade[c]).date(),
                         "diff_days": int(d_rows.size), "only_proto_days": int(only_p.sum()), "only_ours_days": int((~only_p).sum()),
                         **counts, "dominant_cause": dominant})
    df = pd.DataFrame(rows).sort_values(["universe", "diff_days"], ascending=[True, False])
    df.to_csv(out_dir / "universe_diff_items.csv", index=False)
    return df


def cause_summary(items: pd.DataFrame) -> pd.DataFrame:
    recs = []
    for u in GATE_UNIVERSES:
        sub = items[items.universe == u]
        for k in CAUSES:
            nd = int(sub[k].sum())
            if nd:
                recs.append({"universe": u, "cause": k, "name_days": nd, "items_with_days": int((sub[k] > 0).sum()),
                             "items_dominant": int((sub.dominant_cause == k).sum())})
    return pd.DataFrame(recs)


def tradable_effect(axes: Axes, work: Path, full_run: str, notrad_run: str, others: list[str], out_dir: Path) -> pd.DataFrame:
    """full (strict tradable + PIT primary) and comparison runs vs notrad (prototype-equivalent pool)."""
    info_full = read_json(work / "runs" / full_run / "run.json")
    ti = info_full.get("tradable_info") or {}
    lines = ["# tradable 적용 효과", "",
             f"생성 {dt.datetime.now():%Y-%m-%d %H:%M:%S}. 대조 = `{notrad_run}`(lifecycle 미적용, primary 현재값 = 프로토 동등). "
             f"`{full_run}` = strict tradable(lifecycle 창 ∧ close notna ∧ ¬exclusions 를 랭킹 전 후보에 적용) + PIT primary(currentPrimary 이거나 "
             "relist/rename 체인으로 currentPrimary 종목의 선행 id, 후속 id 거래 시작 전까지). "
             "2010-01-01 이후 거래일. removed = 대조에서만 멤버, added = 적용에서만 멤버(id 수준). line 수준 = 옛 id 를 계보 끝 id 열로 합친 뒤 비교(회사 단위).", "",
             "## 1. tradable 마스크", "", md_table(pd.DataFrame([{"항목": k, "값": v} for k, v in ti.items()])), "",
             "PIT primary: " + str(info_full.get("pit_primary")), ""]
    item_rows, recs = [], []
    sel = axes.sel2010.to_numpy()
    base = {variant: load_run_masks(work / "runs" / notrad_run, variant, axes) for variant in ("v1rule", "all")}
    for run_name in [full_run] + others:
        rd = work / "runs" / run_name
        if not (rd / "run.json").exists():
            continue
        info = read_json(rd / "run.json")
        for variant in ("v1rule", "all"):
            if variant not in info["variants"]:
                continue
            a = load_run_masks(rd, variant, axes)
            b = base[variant]
            for u in UNIVERSE_NAMES:
                na, nb = a[u][sel].sum(axis=1), b[u][sel].sum(axis=1)
                removed, added = (b[u] & ~a[u])[sel], (a[u] & ~b[u])[sel]
                al, bl = axes.to_line(a[u])[sel], axes.to_line(b[u])[sel]
                recs.append({"run": run_name, "tradable": info["tradable"], "primary": info.get("primary", "current"), "variant": variant, "universe": u,
                             "members_med": float(np.median(na)), "members_med_notrad": float(np.median(nb)),
                             "daily_diff_med": float(np.median(na - nb)), "daily_diff_min": int((na - nb).min()), "daily_diff_max": int((na - nb).max()),
                             "removed_name_days": int(removed.sum()), "added_name_days": int(added.sum()),
                             "removed_items": int(removed.any(axis=0).sum()), "added_items": int(added.any(axis=0).sum()),
                             "line_removed_name_days": int((bl & ~al).sum()), "line_added_name_days": int((al & ~bl).sum())})
                rd_, ad_ = removed.sum(axis=0), added.sum(axis=0)
                for c in np.flatnonzero((rd_ > 0) | (ad_ > 0)):
                    item_rows.append({"run": run_name, "variant": variant, "universe": u, "tradingItemId": int(axes.ids[c]), "symbol": axes.symbol[c],
                                      "companyName": axes.name[c], "stitched": bool(axes.stitched[c]), "currentPrimary": bool(axes.current_primary[c]),
                                      "removed_days": int(rd_[c]), "added_days": int(ad_[c])})
            del a
    summary = pd.DataFrame(recs)
    items = pd.DataFrame(item_rows)
    items.to_csv(out_dir / "tradable_effect_items.csv", index=False)
    for run_name in [full_run] + others:
        sub = summary[summary.run == run_name]
        if sub.empty:
            continue
        lines += [f"## 2. `{run_name}` vs `{notrad_run}` — 일별 멤버 수·멤버-일 (2010+)", "",
                  md_table(sub.drop(columns=["run"]), "{:.1f}"), ""]
    for run_name in [full_run] + others:
        for variant, u in (("v1rule", "Top500"), ("all", "ALL")):
            sub = items[(items.run == run_name) & (items.variant == variant) & (items.universe == u)]
            if sub.empty:
                continue
            lines += [f"## 3. 영향 종목 상위 20 — `{run_name}` `{variant}` {u} (removed_days 순; 전체 목록 `tradable_effect_items.csv`)", "",
                      md_table(sub.sort_values("removed_days", ascending=False).head(20).drop(columns=["run", "variant", "universe"])), "",
                      "added_days 상위 10:", "",
                      md_table(sub.sort_values("added_days", ascending=False).head(10).drop(columns=["run", "variant", "universe"])), ""]
    (out_dir / "tradable_effect.md").write_text("\n".join(lines) + "\n")
    return summary


def counts_summary(axes: Axes, ours_v1rule: dict, ours_all: dict) -> pd.DataFrame:
    rows = []
    sel = axes.sel2010.to_numpy()
    for u in UNIVERSE_NAMES:
        rec = {"universe": u,
               "T5 membership(all) med 2010+": float(np.median(ours_all[u][sel].sum(axis=1))),
               "T5 membership_v1rule med 2010+": float(np.median(ours_v1rule[u][sel].sum(axis=1))),
               "T5 all last 250d med": float(np.median(ours_all[u][-250:].sum(axis=1))),
               "T5 all items ever": int(ours_all[u].any(axis=0).sum()), "T5 v1rule items ever": int(ours_v1rule[u].any(axis=0).sum())}
        for label, root in (("proto_all", PROTO_ROOT / "TopUniverseV2"), ("proto_v1rule", PROTO_ROOT / "TopUniverseV2_v1rule"),
                            ("v1_snapshot", V1_SNAPSHOT / "TopUniverseV1")):
            p = root / f"{u}.parquet"
            if p.exists():
                t = pq.read_table(p)
                d = pd.to_datetime(t.column("__index_level_0__").to_numpy())
                n = np.zeros(t.num_rows, dtype=np.int64)
                for name in t.column_names:
                    if name != "__index_level_0__":
                        n += t.column(name).to_numpy(zero_copy_only=False).astype(np.int64)
                rec[f"{label} med 2010+"] = float(np.median(n[d >= SINCE]))
        rows.append(rec)
    return pd.DataFrame(rows)


def yearly_counts(axes: Axes, masks: dict, label: str) -> pd.DataFrame:
    df = pd.DataFrame({u: masks[u].sum(axis=1) for u in GATE_UNIVERSES}, index=axes.dates)
    y = df.groupby(df.index.year).median().astype(int)
    y.index.name = "year"
    y.columns = [f"{label} {c}" for c in y.columns]
    return y


def sensitivity(axes: Axes, work: Path, base_run: str, extra_runs: list[str], protos: dict) -> pd.DataFrame:
    """Alternative dollar-volume estimators (same tradable/primary/mcap): overlap vs proto v1rule and vs the base run."""
    base = load_run_masks(work / "runs" / base_run, "v1rule", axes, GATE_UNIVERSES)
    rows = []
    for name in [base_run] + extra_runs:
        rd = work / "runs" / name
        if not (rd / "run.json").exists():
            continue
        info = read_json(rd / "run.json")
        m = load_run_masks(rd, "v1rule", axes, GATE_UNIVERSES)
        rec = {"run": name, "dollar": info["dollar_mode"], "tradable": info["tradable"], "primary": info.get("primary")}
        for u in GATE_UNIVERSES:
            ref, row_valid, _ = protos[u]
            use = row_valid & axes.sel2010.to_numpy()
            st, _ = overlap_stats(m[u], ref, use, axes.dates, name, u)
            st_l, _ = overlap_stats(axes.to_line(m[u]), axes.to_line(ref), use, axes.dates, name, u)
            rec[f"{u}_med"] = st["ours_med"]
            rec[f"{u}_line_ov_proto_med"] = st_l["ov_of_ours_med"]
            rec[f"{u}_line_ov_proto_p05"] = st_l["ov_of_ours_p05"]
            rec[f"{u}_symdiff_proto"] = st["only_ours_name_days"] + st["only_ref_name_days"]
            rec[f"{u}_symdiff_vs_{base_run}"] = int((m[u][use] ^ base[u][use]).sum())
        rows.append(rec)
    return pd.DataFrame(rows)


def counterexample(axes: Axes, work: Path, base_run: str, counter_runs: list[str], protos: dict) -> dict:
    """The overlap metric must fall when a rule parameter is changed (detectability of rule drift)."""
    ref, row_valid, _ = protos["Top500"]
    use = row_valid & axes.sel2010.to_numpy()
    rows, base = [], None
    for name in [base_run] + counter_runs:
        info = read_json(work / "runs" / name / "run.json")
        m = load_run_masks(work / "runs" / name, "v1rule", axes, ("Top500",))["Top500"]
        st, _ = overlap_stats(m, ref, use, axes.dates, name, "Top500")
        changed = {k: v for k, v in info["params"].items() if v != UNIV_PARAMS[k]}
        rows.append({"run": name, "changed_params": str(changed) if changed else "(base)", "Top500_ov_med": st["ov_of_ours_med"],
                     "Top500_ov_p05": st["ov_of_ours_p05"], "symdiff_name_days": st["only_ours_name_days"] + st["only_ref_name_days"]})
        if base is None:
            base = rows[-1]
    drops = [r["Top500_ov_med"] < base["Top500_ov_med"] - 1e-9 or r["symdiff_name_days"] > base["symdiff_name_days"] for r in rows[1:]]
    ok = bool(drops and all(drops))
    return {"rows": rows, "pass": ok,
            "summary": f"기준 run `{base_run}` Top500 중첩 {base['Top500_ov_med']:.4f}(대칭차 {base['symdiff_name_days']:,}) → " + "; ".join(
                f"{r['run']} {r['changed_params']}: {r['Top500_ov_med']:.4f} (대칭차 {r['symdiff_name_days']:,})" for r in rows[1:])
                       + (" → 파라미터 변경이 중첩 하락/대칭차 증가로 검출됨" if ok else " → 검출 실패")}


EXAMPLE_EDGES = {"BLK": (2594723, 1899273640), "GOOGL": (312940216, 11311662), "JCI": (2623229, 372277281),
                 "XRX": (2665260, 610592433), "CCO": (24687598, 618650356)}


def transition_gate(axes: Axes, work: Path, run_names: list[str], universes=GATE_UNIVERSES) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Continuity of line membership across relist/rename transitions (lead gate 2), from the build's
    `transitions_v1rule.parquet` (per edge: membership and pre-sticky eligibility on the old id's last day and the
    new id's first day).

    transition_caused_gap = old member on last day & new not member on first day & line pre-eligible on both days
    (nothing in the rule inputs explains the drop) -> must be 0.  Other non-continuous transitions are explained:
    rule_exit (line not pre-eligible on the first day: dollar volume / market cap / no cap), pool_exit (new id outside
    the variant's candidate pool or not tradable), data_gap (trading days without any id between old and new)."""
    rows, ex_rows = [], []
    for name in run_names:
        p = work / "runs" / name / "transitions_v1rule.parquet"
        if not p.exists():
            continue
        d = pd.read_parquet(p)
        for u in universes:
            t = d[d.universe == u]
            om = t[t.old_member_last]
            nc = om[~om.new_member_first]
            caused = nc[nc.pre_last & nc.pre_first]
            rule_exit = nc[~nc.pre_first & nc.new_in_pool_first]
            pool_exit = nc[~nc.new_in_pool_first]
            rows.append({"run": name, "universe": u, "edges": int(len(t)), "edges_data_gap": int((t.data_gap_days > 0).sum()),
                         "old_member_on_last_day": int(len(om)), "continuous": int(om.new_member_first.sum()),
                         "not_continuous": int(len(nc)), "transition_caused_gap": int(len(caused)),
                         "rule_exit_first_day": int(len(rule_exit)), "pool_exit_new_id": int(len(pool_exit)),
                         "other": int(len(nc) - len(caused) - len(rule_exit) - len(pool_exit)),
                         "gap_days_first_61_total": int(nc.gap_days_first_61.sum()),
                         "gap_days_transition_caused": int(caused.gap_days_first_61.sum())})
            for sym, (o, n) in EXAMPLE_EDGES.items():
                sub = t[(t.oldId == o) & (t.newId == n)]
                if sub.empty:
                    ex_rows.append({"run": name, "universe": u, "symbol": sym, "oldId": o, "newId": n, "note": "edge not in lineage/panel"})
                    continue
                r = sub.iloc[0]
                ex_rows.append({"run": name, "universe": u, "symbol": sym, "oldId": o, "newId": n,
                                "effectiveDate": pd.Timestamp(r.effectiveDate).date(), "old_last_day": pd.Timestamp(r.date_last).date(),
                                "new_first_day": pd.Timestamp(r.date_first).date(), "old_member_on_last_day": bool(r.old_member_last),
                                "new_member_on_first_day": bool(r.new_member_first), "pre_eligible_last/first": f"{bool(r.pre_last)}/{bool(r.pre_first)}",
                                "new_first_member_day": axes.dates[int(r.new_first_member_row)].date() if r.new_first_member_row >= 0 else None,
                                "gap_days_first_61": int(r.gap_days_first_61), "old_member_days": int(r.old_member_days), "new_member_days": int(r.new_member_days)})
    return pd.DataFrame(rows), pd.DataFrame(ex_rows)


def duplicate_gate(axes: Axes, work: Path, run_names: list[str]) -> pd.DataFrame:
    """Double-counting gate: two ids of one lineage line both members on the same day (recomputed here, 2010+ and all)."""
    rows = []
    for name in run_names:
        rd = work / "runs" / name
        info = read_json(rd / "run.json")
        for variant in info["variants"]:
            masks = load_run_masks(rd, variant, axes, GATE_UNIVERSES)
            for u in GATE_UNIVERSES:
                d_all = lineage_duplicates(masks[u], axes.line_col)
                d_2010 = lineage_duplicates(masks[u], axes.line_col, axes.sel2010.to_numpy())
                rows.append({"run": name, "variant": variant, "universe": u, "dup_cells_all": d_all["dup_cells"],
                             "dup_cells_2010plus": d_2010["dup_cells"], "lines_with_dups": d_all["lines_with_dups"],
                             "lines_checked": d_all["lines_checked"]})
    return pd.DataFrame(rows)


def write_gates_md(out_dir: Path, tab: pd.DataFrame, lad: pd.DataFrame, items: pd.DataFrame, causes: pd.DataFrame,
                   counts: pd.DataFrame, yearly: pd.DataFrame, counter: dict, run_info: dict, mcap_cmp: dict | None,
                   sens: pd.DataFrame, dups: pd.DataFrame, trans: pd.DataFrame | None = None,
                   trans_ex: pd.DataFrame | None = None) -> Path:
    def pick(label, cols):
        return md_table(tab[tab.comparison == label][["universe"] + cols].copy(), "{:.4f}")

    ov_cols = ["days", "ours_med", "ref_med", "ov_of_ours_med", "ov_of_ours_p05", "ov_of_ours_min", "ov_of_ref_med",
               "ov_of_ref_p05", "jaccard_med", "jaccard_pooled", "only_ours_name_days", "only_ref_name_days", "ov_of_ours_med_2000plus"]
    g = tab[(tab.comparison == "proto_v1rule") & tab.universe.isin(GATE_UNIVERSES)].set_index("universe")
    gl = tab[(tab.comparison == "proto_v1rule[line]") & tab.universe.isin(GATE_UNIVERSES)].set_index("universe")
    gate_pass = bool((g.ov_of_ours_med >= 0.99).all())
    gate_pass_line = bool((gl.ov_of_ours_med >= 0.99).all())
    notrad = lad[lad.run == "notrad"].iloc[0] if (lad.run == "notrad").any() else None
    rt = all(run_info["variants"][v]["roundtrip_selftest"]["all_pass"] for v in run_info["variants"])
    lchk = run_info.get("loader_check") or {}
    dup_ok = bool((dups[dups.run == run_info["name"]].dup_cells_all == 0).all()) if len(dups) else False
    tr = trans[trans.run == run_info["name"]] if trans is not None and len(trans) else pd.DataFrame()
    tr_ok = bool(len(tr) and (tr.transition_caused_gap == 0).all())
    lines = [
        "# universe 게이트 (T5)", "",
        f"생성 {dt.datetime.now():%Y-%m-%d %H:%M:%S}. run `{run_info['name']}`: tradable={run_info['tradable']}, primary={run_info.get('primary')}, "
        f"dollar={run_info['dollar_mode']}, mcap={run_info['mcap_source']}, params={run_info['params']}, T={run_info['T']} N={run_info['N']} "
        f"({run_info['dates'][0]}~{run_info['dates'][1]}). 중첩 = 일별 |A∩B|/|A| (A = T5, B = 참조), 2010-01-01 이후 두 쪽 모두 있는 거래일(days). p05 = 5퍼센타일. "
        "line 수준 = 옛 id 의 멤버-일을 lineage(relist/rename, high/medium) 체인 끝 id 열로 합친 뒤 비교.", "",
        "## 0. 판정", "",
        md_table(pd.DataFrame([
            {"게이트": "Top500 중첩 |A∩B|/|A| 중앙값 ≥ 0.99 vs 프로토 TopUniverseV2_v1rule (id 수준)",
             "값": f"{g.loc['Top500', 'ov_of_ours_med']:.4f} (p05 {g.loc['Top500', 'ov_of_ours_p05']:.4f}, 최소 {g.loc['Top500', 'ov_of_ours_min']:.4f}, n={int(g.loc['Top500', 'days'])}일)",
             "판정": "PASS" if g.loc["Top500", "ov_of_ours_med"] >= 0.99 else "FAIL"},
            {"게이트": "Top500·Top1000·ALL 중앙값 모두 ≥ 0.99 (id 수준)", "값": ", ".join(f"{u} {g.loc[u, 'ov_of_ours_med']:.4f}" for u in GATE_UNIVERSES),
             "판정": "PASS" if gate_pass else "FAIL"},
            {"게이트": "같은 비교 line 수준(회사 단위)", "값": ", ".join(f"{u} {gl.loc[u, 'ov_of_ours_med']:.4f} (p05 {gl.loc[u, 'ov_of_ours_p05']:.4f})" for u in GATE_UNIVERSES),
             "판정": "PASS" if gate_pass_line else "FAIL"},
            {"게이트": "notrad(lifecycle 미적용·primary 현재값) 의 프로토 재현 — id 수준 대칭차 멤버-일",
             "값": (", ".join(f"{u} {int(notrad[f'{u}_symdiff']):,}" for u in GATE_UNIVERSES) if notrad is not None else "n/a"),
             "판정": ("PASS" if notrad is not None and notrad["Top500_symdiff"] == 0 else "확인")},
            {"게이트": "이중 계산: 같은 lineage 라인의 두 id 가 같은 날 둘 다 멤버인 셀 = 0 (Top500·Top1000·ALL × 2 변형, 전 기간)",
             "값": str(dups[dups.run == run_info["name"]][["variant", "universe", "dup_cells_all"]].to_dict("records")) if len(dups) else "n/a",
             "판정": "PASS" if dup_ok else "FAIL"},
            {"게이트": "전환 연속성: 옛 id 마지막 활성일 멤버 → 새 id 첫날 멤버; 전환 때문에 생긴 공백(양쪽 다 규칙상 적격인데 끊김) = 0 (v1rule, Top500·Top1000·ALL)",
             "값": "; ".join(f"{r.universe} 연속 {int(r.continuous)}/{int(r.old_member_on_last_day)}, 전환 유발 공백 {int(r.transition_caused_gap)}, 규칙 탈락(첫날 부적격) {int(r.rule_exit_first_day)}, 풀 이탈(새 id 후보 밖) {int(r.pool_exit_new_id)}" for r in tr.itertuples()) if len(tr) else "n/a",
             "판정": "PASS" if tr_ok else "FAIL"},
            {"게이트": "구간 압축 역변환 비트 동일(8 universe × 2 변형) + 심은 반례 검출", "값": str(rt), "판정": "PASS" if rt else "FAIL"},
            {"게이트": "참조 로더 get_mask == 역변환 (캘린더 날짜; Top500, ALL)", "값": str({k: v for k, v in lchk.items() if not k.endswith("dates")}),
             "판정": "PASS" if lchk and all(v for k, v in lchk.items() if not k.endswith("dates")) else "n/a"},
            {"게이트": "반례 self-test: 파라미터 변경 시 중첩 하락", "값": counter.get("summary", ""), "판정": "PASS" if counter.get("pass") else "FAIL"},
        ])), "",
        "## 1. (a) membership_v1rule vs 프로토 `TopUniverseV2_v1rule` (2010+) — id 수준", "", pick("proto_v1rule", ov_cols), "",
        "line 수준:", "", pick("proto_v1rule[line]", ov_cols), "",
        "## 2. membership(전체 변형) vs 프로토 `TopUniverseV2` (2010+) — id 수준", "", pick("proto_all", ov_cols), "",
        "line 수준:", "", pick("proto_all[line]", ov_cols), "",
        "## 3. (b) v1 스냅샷 `TopUniverseV1` 대비 (참고; ref_name_days_outside_panel = v1 열 중 우리 패널 밖 종목의 멤버-일)", "",
        pick("v1_snapshot(v1rule)", ov_cols + ["ref_name_days_outside_panel"]), "", "전체 변형:", "",
        pick("v1_snapshot(all)", ov_cols + ["ref_name_days_outside_panel"]), "",
        "## 4. (c) 일별 멤버 수 요약 (중앙값)", "", md_table(counts, "{:.0f}"), "", "연도별 중앙값(Top500·Top1000·ALL):", "",
        md_table(yearly.reset_index(), "{:.0f}"), "",
        "## 5. 차이 원인 분해 — ablation ladder (v1rule 변형, 2010+, 프로토 v1rule 대비)", "",
        "각 단계에서 조건 하나를 프로토 쪽으로 되돌림: strict_pit = 라인 계산 대신 id 별 strict(재상장 시 sticky 재시작), strict_curprim = primary 를 현재값으로(PIT 해제; 재상장 회사가 이어붙인 기간 내내 사라지는 반사실), "
        "notrad = lifecycle 미적용(프로토 동등 후보), notrad_sadj = 달러거래대금을 split-adj 기준(프로토 산식)으로, notrad_sadj_pmcap = 시총도 프로토 패널로. "
        "`_symdiff` = 대칭차 멤버-일수(id 수준), `_line_*` = line 수준, `_vs_prev` = 직전 단계 대비 바뀐 멤버-일수.", "",
        md_table(lad, "{:.4f}"), "",
        "## 6. 차이 종목 원인 분류 (v1rule 변형 vs 프로토, 2010+, 대칭차 > 0 종목; 일 단위 원인별 집계)", "",
        "원인: reattribution_old_new_id = 같은 회사, 프로토는 이어붙인 새 id·T5 는 그 날 실제 거래 id(line 수준 일치) / transition_gap_sticky_restart = 재상장 새 id 의 firstTradeDate 뒤 "
        "StickyEntryDays+1 거래일(sticky 진입 재시작) / stitched_old_not_in_panel = 옛 id 가 종목 집합에 없음 / stitched_old_not_member = 옛 id 는 있으나 규칙 미충족 / "
        "tail_cut = lastTradeDate 뒤 꼬리 / exclusion = tradable_exclusions / pit_predecessor_added = PIT primary 로 들어온 선행 id(프로토엔 그 라인 없음) / new_id = 프로토에 없는 종목 / "
        "rank_boundary_substitution = lifecycle·PIT 를 끄면 사라지는 잔차(후보 집합 변화로 순위·임계 경계에서 들고 난 종목) / dollar_volume_basis·mcap_diff = ladder 단계에서 사라짐 / other = 나머지.", "",
        md_table(causes) if len(causes) else "차이 없음", "",
    ]
    if len(items):
        show = ["tradingItemId", "symbol", "companyName", "stitched", "currentPrimary", "firstTradeDate", "lastTradeDate", "diff_days",
                "only_proto_days", "only_ours_days", "dominant_cause"]
        for u in GATE_UNIVERSES:
            lines += [f"### {u} 차이 상위 25 (diff_days 순)", "", md_table(items[items.universe == u].head(25)[show]), ""]
        lines += ["전체 목록(원인별 일수 포함) `universe_diff_items.csv`", ""]
    if trans is not None and len(trans):
        lines += ["## 6b. 전환 연속성 게이트 (run 별, v1rule 변형, 전 기간)", "",
                  "edge = lineage relist/rename(high/medium) old→new(둘 다 패널). old_member_on_last_day = 옛 id 가 라인의 마지막 활성일에 멤버인 edge 수, continuous = 그중 새 id 가 firstTradeDate 당일 멤버. "
                  "not_continuous 분해: transition_caused_gap = 양쪽 날 모두 규칙상 적격(pre-sticky 선택)인데 끊김(전환 메커니즘 탓; 목표 0), rule_exit_first_day = 첫날 라인이 부적격(달러거래대금·시총 결측 등, 단일 종목이라도 같은 결과), "
                  "pool_exit_new_id = 새 id 가 변형의 후보 풀 밖(예 OTC 이전·.Q). gap_days_* = 첫 61거래일 중 어느 id 도 멤버가 아닌 일수.", "",
                  md_table(trans), ""]
        if trans_ex is not None and len(trans_ex):
            lines += ["사례(BLK·GOOGL·JCI·XRX·CCO), Top500:", "", md_table(trans_ex[trans_ex.universe == "Top500"]), ""]
    lines += ["## 7. 이중 계산 게이트 (run 별)", "", md_table(dups), "",
              "## 8. (d) 반례 self-test", "", md_table(pd.DataFrame(counter.get("rows", []))), "", counter.get("summary", ""), ""]
    if sens is not None and len(sens):
        lines += ["## 9. 달러거래대금 추정식 민감도 (v1rule 변형, tradable·primary 동일, 2010+)", "",
                  "raw_meanxmean = v1 산식을 raw 입력에 그대로(기본), split_adj_meanxmean = 분할계수로 한 기준에 맞춘 뒤 v1 산식(프로토와 같은 산식), "
                  "raw_product_mean = 일별 raw close×volume 의 100일 평균(기준 무관 산식).", "", md_table(sens, "{:.4f}"), ""]
    if mcap_cmp:
        lines += ["## 10. marketCap 대조 요약 (상세 `marketcap_compare.md`)", "",
                  md_table(pd.DataFrame([{"항목": k, "값": v} for k, v in mcap_cmp.items()
                                         if k in ("proto_cells", "common_cells", "only_ours_in_span", "only_proto", "rel_diff_gt_tol",
                                                  "rel_diff_gt_tol_frac", "rel_diff_median", "rel_diff_max", "bad_by_cause")])), ""]
    p = out_dir / "universe_gates.md"
    p.write_text("\n".join(lines) + "\n")
    return p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["all"])
    ap.add_argument("--run", default="full")
    ap.add_argument("--notrad", default="notrad")
    ap.add_argument("--ladder", default="strict_pit,strict_curprim,notrad,notrad_sadj,notrad_sadj_pmcap")
    ap.add_argument("--counter", default="notrad_entry30,notrad_dv1e6")
    ap.add_argument("--extra", default="full_sadj,full_product", help="sensitivity runs (dollar-volume estimator)")
    ap.add_argument("--others", default="strict_pit,strict_curprim", help="extra runs for tradable_effect.md / gates")
    ap.add_argument("--t2", type=Path, default=T2_ROOT)
    ap.add_argument("--master", type=Path, default=T1_MASTER)
    ap.add_argument("--t5", type=Path, default=T5_ROOT)
    ap.add_argument("--work", type=Path, default=T5_WORK)
    args = ap.parse_args(argv)
    out_dir = args.t5 / "qa"
    out_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(args.work / "qa.log")
    tm = Timer("universe_qa")
    axes = Axes(args.t2, args.master, args.work)
    full_dir = args.work / "runs" / args.run
    run_info = read_json(full_dir / "run.json")
    ours_v1rule = load_run_masks(full_dir, "v1rule", axes)
    ours_all = load_run_masks(full_dir, "all", axes)
    tab = gate_overlaps(axes, ours_v1rule, ours_all, out_dir)
    tab.to_csv(out_dir / "universe_overlap_summary.csv", index=False)
    counts = counts_summary(axes, ours_v1rule, ours_all)
    yearly = pd.concat([yearly_counts(axes, ours_all, "all"), yearly_counts(axes, ours_v1rule, "v1rule")], axis=1)
    del ours_v1rule, ours_all
    ladder_names = [args.run] + [x for x in args.ladder.split(",") if x]
    lad, per_item, protos = ladder(axes, ladder_names, args.work)
    items = classify_items(axes, args.work, args.run, ladder_names, per_item, protos, out_dir)
    causes = cause_summary(items) if len(items) else pd.DataFrame()
    counter = counterexample(axes, args.work, args.notrad, [x for x in args.counter.split(",") if x], protos)
    sens = sensitivity(axes, args.work, args.run, [x for x in args.extra.split(",") if x], protos)
    sens.to_csv(out_dir / "universe_sensitivity.csv", index=False)
    others = [x for x in args.others.split(",") if x and (args.work / "runs" / x / "run.json").exists()]
    trad = tradable_effect(axes, args.work, args.run, args.notrad, others, out_dir)
    dups = duplicate_gate(axes, args.work, [args.run] + [x for x in others if x != args.run])
    dups.to_csv(out_dir / "universe_duplicate_gate.csv", index=False)
    trans, trans_ex = transition_gate(axes, args.work, [args.run] + [x for x in others if x != args.run])
    trans.to_csv(out_dir / "universe_transition_gate.csv", index=False)
    trans_ex.to_csv(out_dir / "universe_transition_examples.csv", index=False)
    mcap_cmp = read_json(args.work / "marketcap_compare.json")
    p = write_gates_md(out_dir, tab, lad, items, causes, counts, yearly, counter, run_info, mcap_cmp, sens, dups, trans, trans_ex)
    write_json(args.work / "qa_summary.json", {"overlap": tab.to_dict("records"), "ladder": lad.to_dict("records"),
                                               "counter": counter, "counts": counts.to_dict("records"),
                                               "sensitivity": sens.to_dict("records"), "yearly": yearly.reset_index().to_dict("records"),
                                               "causes": causes.to_dict("records") if len(causes) else [],
                                               "tradable_effect": trad.to_dict("records"), "duplicates": dups.to_dict("records"),
                                               "transition": trans.to_dict("records"), "transition_examples": trans_ex.to_dict("records")})
    log.info("gates written: %s", p)
    tm.done()
    return 0


if __name__ == "__main__":
    sys.exit(main())
