#!/home/jgo/usr/venv-3.11/bin/python
"""cache_v2.1 publish (T8, lead): assemble stage outputs into one build, apply lifecycle masking,
write manifest with sha256 per file, and (optionally) sync to the 61 server atomically.

Layout produced (BRIEF §3):
  base_data.parquet            T2, rows after lifecycle.lastTradeDate get qualityFlag=4 (kept, flagged)
  factors/{split,dividend}.parquet
  derived/returns_total.parquet  T2, rows after lastTradeDate DROPPED
  master/*.parquet             T1
  universe/membership.parquet  T5 (optional)
  derived/marketcap.parquet    T5 (optional)
  events/corporate_actions.parquet  T6 (optional)
  dividends/, distributions/   copied from prototype v2 (as-paid USD tables; cache-unit columns refer to the proto basis)
  qa/                          reports + task qa dirs
  manifest.json, README.md

Never writes under /home/base. Remote publish goes to <remote>.incoming-<build_id> then renames.
"""
from __future__ import annotations
import argparse, datetime as dt, hashlib, json, os, shutil, subprocess, sys, time
from pathlib import Path
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

QF_POST_LIFECYCLE = 4
PROTO = Path('/home/jgo/61/data2/temp_stock/cache_v2_cash_1d')


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(32 << 20), b''):
            h.update(b)
    return h.hexdigest()


def parquet_rows(path: Path) -> int:
    return pq.ParquetFile(path).metadata.num_rows


def log(msg):
    print(f'[{dt.datetime.now():%H:%M:%S}] {msg}', flush=True)


def load_lifecycle(t1: Path) -> pd.DataFrame:
    lc = pd.read_parquet(t1 / 'master' / 'lifecycle.parquet')
    need = {'tradingItemId', 'lastTradeDate', 'firstTradeDate', 'status'}
    missing = need - set(lc.columns)
    if missing:
        raise SystemExit(f'lifecycle missing columns {missing}')
    lc['tradingItemId'] = lc.tradingItemId.astype('int64')
    lc['lastTradeDate'] = pd.to_datetime(lc.lastTradeDate)
    return lc.set_index('tradingItemId')


def mask_base_data(src: Path, dst: Path, lc: pd.DataFrame) -> dict:
    """Stream row groups; set qualityFlag=4 where date > lastTradeDate of that id. Fail if an id lacks lifecycle."""
    pf = pq.ParquetFile(src)
    last = lc.lastTradeDate
    writer = None
    n_flag = 0; n_rows = 0; unknown = set()
    for rg in range(pf.num_row_groups):
        t = pf.read_row_group(rg)
        ids = t.column('tradingItemId').to_numpy()
        dates = t.column('date').to_numpy().astype('datetime64[ns]')
        ltd = last.reindex(ids).to_numpy().astype('datetime64[ns]')
        unk = pd.isna(ltd)
        if unk.any():
            unknown.update(np.unique(ids[unk]).tolist())
        post = (~unk) & (dates > ltd)
        qf = t.column('qualityFlag').to_numpy().copy()
        qf[post] = QF_POST_LIFECYCLE
        t = t.set_column(t.schema.get_field_index('qualityFlag'), 'qualityFlag', pa.array(qf, type=pa.int8()))
        if writer is None:
            writer = pq.ParquetWriter(dst, t.schema, compression='zstd')
        writer.write_table(t, row_group_size=max(1, t.num_rows))
        n_flag += int(post.sum()); n_rows += t.num_rows
    writer.close()
    return dict(rows=n_rows, flagged_post_lifecycle=n_flag, ids_without_lifecycle=sorted(unknown))


def mask_returns(src: Path, dst: Path, lc: pd.DataFrame) -> dict:
    pf = pq.ParquetFile(src)
    last = lc.lastTradeDate
    writer = None; n_in = 0; n_out = 0
    for rg in range(pf.num_row_groups):
        t = pf.read_row_group(rg)
        ids = t.column('tradingItemId').to_numpy()
        dates = t.column('date').to_numpy().astype('datetime64[ns]')
        ltd = last.reindex(ids).to_numpy().astype('datetime64[ns]')
        keep = pd.isna(ltd) | (dates <= ltd)
        t2 = t.filter(pa.array(keep))
        if writer is None:
            writer = pq.ParquetWriter(dst, t.schema, compression='zstd')
        writer.write_table(t2, row_group_size=max(1, t2.num_rows))
        n_in += t.num_rows; n_out += t2.num_rows
    writer.close()
    return dict(rows_in=n_in, rows_out=n_out, dropped_post_lifecycle=n_in - n_out)


def apply_calendar(t2: Path, out: Path, work: Path, ledger: Path | None = None, returns_legacy: bool = False, lineage: Path | None = None,
                   ledger_stale_after: str | None = None, lifecycle: Path | None = None, legacy_links: bool = False):
    """Return (base_data path, returns path, info). Writes calendar.parquet to out; if off-calendar rows exist,
    writes a filtered base_data into a temp stage. returns_total is ALWAYS regenerated here with
    prices_returns.build_returns (rc2: cash-style v2 from the corporate-action ledger `ledger`; `returns_legacy`
    keeps the rc1 vendor-factor computation) so the published file never carries a stage file of another convention."""
    src = t2 / 'base_data.parquet'
    t = pq.read_table(src, columns=['date', 'close'])
    d = t.column('date').to_numpy().astype('datetime64[ns]'); c = t.column('close').to_numpy()
    df = pd.DataFrame({'date': d, 'has': ~np.isnan(c)})
    n = df.groupby('date')['has'].sum().sort_index()
    med = n.rolling(41, center=True, min_periods=5).median()
    is_trading = n >= 0.2 * med
    cal = pd.DataFrame({'date': n.index, 'nClose': n.values.astype('int64'), 'rollingMedian': med.values, 'isTradingDay': is_trading.values})
    cal.to_parquet(out / 'calendar.parquet', index=False)
    off = cal[~cal.isTradingDay]
    info = dict(dates=int(len(cal)), trading_days=int(is_trading.sum()), off_calendar_dates=int(len(off)))
    stage = out / '_calendar_stage'; (stage / 'factors').mkdir(parents=True); (stage / 'derived').mkdir()
    for f in ['split', 'dividend']:
        os.symlink(t2 / 'factors' / f'{f}.parquet', stage / 'factors' / f'{f}.parquet')
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from prices_returns import build_returns
    if len(off) == 0:
        os.symlink(src, stage / 'base_data.parquet')
        rinfo = build_returns(stage, stage / 'derived' / 'returns_total.parquet', ledger=ledger, legacy=returns_legacy, lineage=lineage,
                              stale_after=ledger_stale_after, links_out=out / 'qa' / 'returns_links.csv', lifecycle=lifecycle, legacy_links=legacy_links)
        info['returns_rebuilt'] = {k: v for k, v in (rinfo or {}).items() if isinstance(v, (int, float, str, dict))}
        return stage / 'base_data.parquet', stage / 'derived' / 'returns_total.parquet', info
    off_dates = set(pd.to_datetime(off.date).values.astype('datetime64[ns]'))
    pf = pq.ParquetFile(src); writer = None; removed = []
    for rg in range(pf.num_row_groups):
        tb = pf.read_row_group(rg)
        dates = tb.column('date').to_numpy().astype('datetime64[ns]')
        drop = np.isin(dates, np.array(sorted(off_dates), dtype='datetime64[ns]'))
        if drop.any():
            removed.append(tb.filter(pa.array(drop)).to_pandas())
        tb = tb.filter(pa.array(~drop))
        if writer is None:
            writer = pq.ParquetWriter(stage / 'base_data.parquet', tb.schema, compression='zstd')
        writer.write_table(tb, row_group_size=max(1, tb.num_rows))
    writer.close()
    rem = pd.concat(removed) if removed else pd.DataFrame()
    rem.to_csv(out / 'qa' / 'off_calendar_rows.csv', index=False)
    info['off_calendar_rows_removed'] = int(len(rem))
    rinfo = build_returns(stage, stage / 'derived' / 'returns_total.parquet', ledger=ledger, legacy=returns_legacy, lineage=lineage,
                          stale_after=ledger_stale_after, links_out=out / 'qa' / 'returns_links.csv', lifecycle=lifecycle, legacy_links=legacy_links)
    info['returns_rebuilt'] = {k: v for k, v in (rinfo or {}).items() if isinstance(v, (int, float, str, dict))}
    return stage / 'base_data.parquet', stage / 'derived' / 'returns_total.parquet', info



def integrate_rc2(stage_rc2: Path, t1: Path, t2: Path, out: Path) -> tuple[Path, Path, dict]:
    """rc2 (orch_rc2/BRIEF.md) inputs -> a T2-like stage under out/_rc2_stage and the lifecycle used for masking.
    (1) base_data = T2 base_data + R3 base_data_aux (master_aux.merge_base_data keeps the global (id, date) order)
    (2) factors = T2 factors + R3 factors_aux (disjoint ids, interval contract re-checked)
    (3) lifecycle = R3 patched lifecycle + lifecycle_aux (status 'aux'); aux ids are added to tradable_exclusions
    Returns (t2_like dir, lifecycle path, info). Missing rc2 inputs fall back to the rc1 files with a note."""
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from prices_factors import check_intervals
    info = {}
    r3 = stage_rc2 / 'R3'
    st = out / '_rc2_stage'; (st / 'factors').mkdir(parents=True); (st / 'master').mkdir()
    aux = r3 / 'base_data_aux.parquet'
    if aux.exists():
        from master_aux import merge_base_data
        info['base_data_aux'] = merge_base_data(t2 / 'base_data.parquet', aux, st / 'base_data.parquet')
    else:
        os.symlink(t2 / 'base_data.parquet', st / 'base_data.parquet'); info['base_data_aux'] = 'absent'
    for f in ['split', 'dividend']:
        base = pd.read_parquet(t2 / 'factors' / f'{f}.parquet')
        add = r3 / 'factors_aux' / f'{f}.parquet'
        if add.exists():
            a = pd.read_parquet(add)
            dup = set(a.tradingItemId) & set(base.tradingItemId)
            if dup:
                raise SystemExit(f'GATE FAIL: factors_aux/{f} ids already in factors: {sorted(dup)[:10]}')
            base = pd.concat([base, a[base.columns]], ignore_index=True).sort_values(['tradingItemId', 'validFrom'])
            chk = check_intervals(base)
            info[f'factors_aux_{f}'] = {'rows': int(len(a)), 'ok': bool(chk.get('ok')), 'violations': chk.get('violations')}
            if not chk.get('ok'):
                raise SystemExit(f"GATE FAIL: factors/{f} interval contract after aux append: {chk.get('violations')} {chk.get('examples', [])[:3]}")
        base.to_parquet(st / 'factors' / f'{f}.parquet', index=False)
    lc_src = r3 / 'master' / 'lifecycle.parquet' if (r3 / 'master' / 'lifecycle.parquet').exists() else t1 / 'master' / 'lifecycle.parquet'
    lc = pd.read_parquet(lc_src)
    lca = r3 / 'master' / 'lifecycle_aux.parquet'
    if lca.exists():
        a = pd.read_parquet(lca)
        if set(a.tradingItemId) & set(lc.tradingItemId):
            raise SystemExit('GATE FAIL: lifecycle_aux ids overlap lifecycle')
        lc = pd.concat([lc, a[lc.columns]], ignore_index=True)
        lc['tradingItemStatusId'] = lc['tradingItemStatusId'].astype('Int64')
        info['lifecycle_aux_rows'] = int(len(a))
    lc.to_parquet(st / 'master' / 'lifecycle.parquet', index=False)
    # aux ids are never tradable: extend tradable_exclusions (loader applies it to the tradable mask)
    ex_src = t1 / 'master' / 'tradable_exclusions.parquet'
    ex = pd.read_parquet(ex_src) if ex_src.exists() else pd.DataFrame(columns=['tradingItemId', 'fromDate', 'toDate', 'reason', 'preferredItemId'])
    if lca.exists():
        a = pd.read_parquet(lca)
        add = pd.DataFrame({'tradingItemId': a.tradingItemId.astype('int64'), 'fromDate': pd.Timestamp('1900-01-01'), 'toDate': pd.Timestamp('2100-01-01'),
                            'reason': 'aux_instrument', 'preferredItemId': pd.array([pd.NA] * len(a), dtype='Int64')})
        ex = pd.concat([ex, add], ignore_index=True)
        ex['preferredItemId'] = ex['preferredItemId'].astype('Int64')
    ex.to_parquet(st / 'master' / 'tradable_exclusions.parquet', index=False)
    info['lifecycle_src'] = str(lc_src)
    return st, st / 'master' / 'lifecycle.parquet', info

def copy(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def sync_remote(out: Path, remote: Path, build_id: str, files: dict) -> dict:
    """Atomic remote publish: rsync -> <remote>.incoming-<id>, sha256 of every manifest file re-checked on the remote, rename(remote ->
    .prev-<prev_id>), rename(incoming -> remote). Separated from the build (D2 rework P1-3) so update_daily can gate the local output
    (engine export anchors) BEFORE anything reaches the remote: `publish.py --sync-only --out <local build> --remote <dir>`."""
    remote = Path(remote)
    incoming = remote.with_name(remote.name + f'.incoming-{build_id}')
    log(f'rsync -> {incoming}')
    subprocess.run(['rsync', '-rlt', '--no-owner', '--no-group', '--no-perms', '--info=stats1', str(out) + '/', str(incoming) + '/'], check=True)
    bad = [rel for rel, e in files.items() if sha256(incoming / rel) != e['sha256']]
    if bad:
        raise SystemExit(f'remote sha256 mismatch: {bad[:5]}')
    prev_id = None
    if remote.exists():
        prev_id = json.loads((remote / 'manifest.json').read_text()).get('build_id', 'unknown') if (remote / 'manifest.json').exists() else 'unknown'
        os.rename(remote, remote.with_name(remote.name + f'.prev-{prev_id}'))
    os.rename(incoming, remote)
    log(f'published {remote}')
    return {'remote': str(remote), 'prev_build_id': prev_id, 'files_checked': len(files)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--stage', default='/tmp/claude-1008/-home-jgo/3f2abe1d-a2a7-4017-9ac1-2c7cb961e311/scratchpad/stage_v21')
    ap.add_argument('--orch', default='/tmp/claude-1008/-home-jgo/3f2abe1d-a2a7-4017-9ac1-2c7cb961e311/scratchpad/orch_v21')
    ap.add_argument('--out', required=True, help='local build dir (created; must not exist)')
    ap.add_argument('--remote', default=None, help='final dir on 61, e.g. /home/jgo/61/data2/temp_stock/cache_v2.1_cash_1d')
    ap.add_argument('--build-id', default=None)
    ap.add_argument('--no-proto-tables', action='store_true')
    ap.add_argument('--ledger', default=None, help='corporate-action ledger for returns_total v2 (default <stage>/R1/events/ca_ledger.parquet, then <stage>/T6/events/ca_ledger.parquet)')
    ap.add_argument('--returns-legacy', action='store_true', help='rc1 vendor-factor returns_total instead of the cash-style v2 (no ledger needed)')
    ap.add_argument('--lineage', default=None, help='lineage file for returns_total first-row links (default <stage>/R3/master/lineage.parquet, then <stage>/T1/master/lineage.parquet)')
    ap.add_argument('--rc2-stage', default='/tmp/claude-1008/-home-jgo/3f2abe1d-a2a7-4017-9ac1-2c7cb961e311/scratchpad/stage_rc2', help='rc2 stage (R1 ledger/export, R2, R3 master/aux/events, V1/V2 qa); absent dir = rc1-only build')
    ap.add_argument('--orch-rc2', default='/tmp/claude-1008/-home-jgo/3f2abe1d-a2a7-4017-9ac1-2c7cb961e311/scratchpad/orch_rc2')
    ap.add_argument('--orch-rc2f', default='/tmp/claude-1008/-home-jgo/3f2abe1d-a2a7-4017-9ac1-2c7cb961e311/scratchpad/orch_rc2f', help='rc2f orchestration dir (BRIEF/TASKS/LEAD_NOTES/reports/registry) copied to qa/rc2f when the rc2 stage carries rc2f_apply_info.json')
    ap.add_argument('--ledger-stale-after', default=None, help='YYYY-MM-DD: the ledger covers events only up to this date (update_daily --allow-stale-ledger); later rows get retFlag 4 and the manifest records it')
    ap.add_argument('--t2', default=None, help='rc2f: dir replacing <stage>/T2 (base_data.parquet + factors/{split,dividend}.parquet with price/split overrides applied by rc2f_apply.py apply; everything else still comes from --stage)')
    ap.add_argument('--legacy-links', action='store_true', help='rc2 lineage link rule for returns_total (first row, chainable ignored) — rc2f bit-identity gate only')
    ap.add_argument('--keep-unmasked-returns', default=None, help='rc2f: also copy the pre-mask returns_total (all close-notna rows, before the lifecycle cut) to this path for returns_v2_qa (rc2 R2 stage layout)')
    ap.add_argument('--sync-only', action='store_true', help='no build: sync an existing local build (--out, with manifest.json) to --remote atomically (D2: after the anchor gate)')
    a = ap.parse_args()
    stage = Path(a.stage); out = Path(a.out)
    if a.sync_only:
        if not a.remote or not (out / 'manifest.json').exists():
            raise SystemExit('--sync-only needs --remote and an existing <out>/manifest.json')
        man = json.loads((out / 'manifest.json').read_text())
        print(json.dumps(sync_remote(out, Path(a.remote), man['build_id'], man['files']), indent=1))
        return
    if out.exists():
        raise SystemExit(f'{out} exists')
    out.mkdir(parents=True)
    build_id = a.build_id or dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    t1, t2 = stage / 'T1', stage / 'T2'
    report = {'build_id': build_id, 'steps': {}}
    if a.t2:
        t2 = Path(a.t2)
        for rel in ('base_data.parquet', 'factors/split.parquet', 'factors/dividend.parquet'):
            if not (t2 / rel).exists():
                raise SystemExit(f'--t2 {t2} lacks {rel}')
        report['steps']['t2_override'] = str(t2); log(f'T2 <- {t2} (rc2f corrected base_data/factors)')

    (out / 'derived').mkdir(); (out / 'factors').mkdir(); (out / 'master').mkdir(); (out / 'qa').mkdir()
    rc2 = Path(a.rc2_stage) if a.rc2_stage and Path(a.rc2_stage).exists() else None
    lc_path = t1 / 'master' / 'lifecycle.parquet'
    if rc2 is not None:
        t2, lc_path, rinfo = integrate_rc2(rc2, t1, t2, out)
        report['steps']['rc2_integration'] = rinfo; log(f'rc2 integration {rinfo}')
    lc = load_lifecycle(lc_path.parent.parent)
    log(f'lifecycle {len(lc)} ids ({lc_path})')
    # --- calendar: trading days = dates whose close count >= 20% of the centered rolling median (prototype rule);
    # rows on other dates (holiday prints of single OTC names) are removed and logged, returns recomputed.
    ledger = Path(a.ledger) if a.ledger else next((p for p in ((rc2 / 'R1' / 'events' / 'ca_ledger.parquet') if rc2 is not None else Path('/nonexistent'), stage / 'R1' / 'events' / 'ca_ledger.parquet', stage / 'T6' / 'events' / 'ca_ledger.parquet') if p.exists()), None)
    if not a.returns_legacy and (ledger is None or not ledger.exists()):
        raise SystemExit('returns_total v2 needs the corporate-action ledger (rc2 BRIEF section 3-1): pass --ledger <events/ca_ledger.parquet> or --returns-legacy')
    lineage = Path(a.lineage) if a.lineage else next((p for p in ((rc2 / 'R3' / 'master' / 'lineage.parquet') if rc2 is not None else Path('/nonexistent'), stage / 'R3' / 'master' / 'lineage.parquet', stage / 'T1' / 'master' / 'lineage.parquet') if p.exists()), None)
    if lineage is not None and not lineage.exists():
        raise SystemExit(f'--lineage {lineage} not found')
    log(f'returns_total: {"rc1 legacy vendor-factor" if a.returns_legacy else f"cash-style v2, ledger {ledger}, lineage {lineage}, stale_after {a.ledger_stale_after}"}')
    base_src, ret_src, cal_info = apply_calendar(t2, out, stage / 'T2_work', ledger=ledger, returns_legacy=a.returns_legacy, lineage=lineage,
                                                 ledger_stale_after=a.ledger_stale_after, lifecycle=lc_path, legacy_links=a.legacy_links)
    report['steps']['calendar'] = cal_info; log(f'calendar {cal_info}')
    r = mask_base_data(base_src, out / 'base_data.parquet', lc)
    report['steps']['base_data'] = r; log(f'base_data {r}')
    if r['ids_without_lifecycle']:
        raise SystemExit(f"GATE FAIL: {len(r['ids_without_lifecycle'])} base_data ids without lifecycle, e.g. {r['ids_without_lifecycle'][:10]}")
    r = mask_returns(ret_src, out / 'derived' / 'returns_total.parquet', lc)
    report['steps']['returns_total'] = r; log(f'returns_total {r}')
    if a.keep_unmasked_returns:
        Path(a.keep_unmasked_returns).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ret_src, a.keep_unmasked_returns); log(f'unmasked returns_total -> {a.keep_unmasked_returns}')
    if base_src.parent.name == '_calendar_stage':
        shutil.rmtree(base_src.parent, ignore_errors=True)
    for f in ['split', 'dividend']:
        copy(t2 / 'factors' / f'{f}.parquet', out / 'factors' / f'{f}.parquet')
    # verification side table (derivable from split intervals; kept so G-SPLIT/reconcile need no DB refetch)
    adjf_side = stage / 'T2_work' / 'adjf_long.parquet'
    if adjf_side.exists():
        cal = pd.read_parquet(out / 'calendar.parquet'); off = np.array(sorted(pd.to_datetime(cal[~cal.isTradingDay].date).values.astype('datetime64[ns]')), dtype='datetime64[ns]')
        pf = pq.ParquetFile(adjf_side); w = None; kept = 0
        for rg in range(pf.num_row_groups):
            tb = pf.read_row_group(rg)
            if len(off):
                keep = ~np.isin(tb.column('date').to_numpy().astype('datetime64[ns]'), off); tb = tb.filter(pa.array(keep))
            if w is None:
                w = pq.ParquetWriter(out / 'factors' / 'adjustment_factor_daily.parquet', tb.schema, compression='zstd')
            w.write_table(tb, row_group_size=max(1, tb.num_rows)); kept += tb.num_rows
        w.close(); log(f'factors/adjustment_factor_daily.parquet rows={kept} (side table, calendar-filtered)')
    else:
        report['steps']['factors/adjustment_factor_daily.parquet'] = 'absent'
    for p in sorted((t1 / 'master').glob('*.parquet')):
        copy(p, out / 'master' / p.name)
    if rc2 is not None:
        # rc2 master overrides: lifecycle (+aux), tradable_exclusions (+aux), lineage v2, lifecycle_dates, aux_instruments
        copy(lc_path, out / 'master' / 'lifecycle.parquet')
        copy(t2 / 'master' / 'tradable_exclusions.parquet', out / 'master' / 'tradable_exclusions.parquet')
        for name in ['lineage.parquet', 'lifecycle_dates.parquet', 'aux_instruments.parquet', 'lifecycle_aux.parquet']:
            src = rc2 / 'R3' / 'master' / name
            if src.exists():
                copy(src, out / 'master' / name); log(f'master/{name} <- R3')
            else:
                report['steps'][f'master/{name}'] = 'absent'
        shutil.rmtree(out / '_rc2_stage', ignore_errors=True)   # merged inputs were consumed above; never published
    # optional wave-2 outputs
    for rel, src in [('universe/membership.parquet', stage / 'T5' / 'universe' / 'membership.parquet'),
                     ('universe/membership_v1rule.parquet', stage / 'T5' / 'universe' / 'membership_v1rule.parquet'),
                     ('derived/marketcap.parquet', stage / 'T5' / 'derived' / 'marketcap.parquet'),
                     ('events/corporate_actions.parquet', stage / 'T6' / 'events' / 'corporate_actions.parquet')]:
        if src.exists():
            copy(src, out / rel); log(f'copied {rel}')
        else:
            report['steps'][rel] = 'absent'; log(f'absent {rel}')
    if rc2 is not None:
        ev = rc2 / 'R3' / 'events' / 'corporate_actions_reclass.parquet'
        if ev.exists():
            copy(ev, out / 'events' / 'corporate_actions.parquet'); log('events/corporate_actions.parquet <- R3 reclass')
            lg = ev.with_name('corporate_actions_reclass.reclass_log.csv')
            if lg.exists():
                copy(lg, out / 'qa' / 'R3' / 'events_reclass_log.csv')
        for name in ['ca_ledger.parquet', 'ca_ledger_repairs.csv', 'ca_ledger_build_info.json', 'ca_ledger_stitched_copies.csv',
                     'ca_ledger_dropped_duplicates.csv', 'ca_ledger_child_not_in_base_data.csv', 'ca_ledger_universe_flags.parquet']:
            src = rc2 / 'R1' / 'events' / name
            if src.exists():
                copy(src, out / 'events' / name)
        if not (out / 'events' / 'ca_ledger.parquet').exists():
            raise SystemExit('GATE FAIL: rc2 build without events/ca_ledger.parquet')
        # engine export is regenerated on the published build (aux ids and merged factors included)
        from loader import CacheV21
        cv = CacheV21(out, master_root=out, threads=8)
        (out / 'engine').mkdir(exist_ok=True)
        m = cv.export_ca_ledger(out / 'engine' / 'ca_ledger_cache.parquet')
        (out / 'engine' / 'ca_ledger_cache_manifest.json').write_text(json.dumps(m, indent=1, default=str))
        report['steps']['engine/ca_ledger_cache.parquet'] = {k: v for k, v in m.items() if not isinstance(v, (list, dict))}
        log(f"engine/ca_ledger_cache.parquet rows={m.get('rows')} sha={str(m.get('sha256'))[:12]}")
        if hasattr(cv, 'export_events'):      # rc2f-E Y1: events in split_adj panel units for the engine `events:` block (SPEC_loader §7)
            me = cv.export_events(out / 'engine' / 'events_cache.parquet')
            (out / 'engine' / 'events_cache_manifest.json').write_text(json.dumps(me, indent=1, default=str))
            report['steps']['engine/events_cache.parquet'] = {k: v for k, v in me.items() if not isinstance(v, (list, dict))}
            log(f"engine/events_cache.parquet rows={me.get('rows')} sha={str(me.get('sha256'))[:12]}")
    if not a.no_proto_tables:
        for rel, src in [('dividends/dividends_long.parquet', PROTO / 'DivData' / 'dividends_long.parquet'),
                         ('distributions/distributions.parquet', PROTO / 'Distributions' / 'distributions.parquet'),
                         ('distributions/research_log.md', PROTO / 'Distributions' / 'research_log.md'),
                         ('distributions/distributions_report.md', PROTO / 'Distributions' / 'distributions_report.md')]:
            if src.exists():
                copy(src, out / rel)
    # updater state (_work): small aggregates/raw factor snapshots so update_daily can run from a published build
    work = out / '_work'; work.mkdir()
    for src in [stage / 'T1' / '_work' / 'ciq_agg.parquet', stage / 'T1' / '_work' / 'mi_agg.parquet', stage / 'T1' / '_work' / 'ciqgvkeyiid_all.parquet',
                stage / 'T1' / '_work' / 'sec_idhist_tic_exchg_isin.parquet', stage / 'T1' / '_work' / 'v1_daily_long.parquet',
                stage / 'T2_work' / 'raw_splitcache.parquet', stage / 'T2_work' / 'raw_divadj.parquet', stage / 'T2_work' / 'raw_dividends.parquet',
                stage / 'T2_work' / 'symbols.parquet', stage / 'T2_work' / 'asof.json', stage / 'T2_work' / 'factors_info.json', stage / 'T2_work' / 'symbol_set.json']:
        if src.exists():
            copy(src, work / src.name)
    (work / 'README.md').write_text('Updater state copied from the build stage (T1/_work, T2_work): per-item DB aggregates, raw factor/dividend snapshots, symbol set. Internal to update_daily.py; not a consumer contract.\n')
    # qa: reports + task qa dirs
    qa = out / 'qa'
    for p in sorted(Path(a.orch, 'reports').glob('*.md')):
        copy(p, qa / 'reports' / p.name)
    for tname in ['T1', 'T2', 'T3', 'T5', 'T6', 'T7', 'T9', 'T10']:
        src = stage / tname / 'qa'
        if src.exists():
            shutil.copytree(src, qa / tname, dirs_exist_ok=True)
    for name, dst in [('gjump_fake_jumps.csv', qa / 'T2' / 'factor_artifacts.csv'), ('gsplit_extreme_intervals.csv', qa / 'T2' / 'gsplit_extreme_intervals.csv'),
                      ('gdiv_mismatches_genuine.csv', qa / 'T2' / 'gdiv_mismatches_genuine.csv'), ('qa_results.json', qa / 'T2' / 'qa_results.json'), ('perf_results.json', qa / 'T2' / 'perf_results.json')]:
        src = stage / 'T2_work' / name
        if src.exists():
            copy(src, dst)
    for name in ['BRIEF.md', 'TASKS.md']:
        if Path(a.orch, name).exists():
            copy(Path(a.orch, name), qa / name)
        else:
            report['steps'][f'qa/{name}'] = f'absent ({a.orch})'
    if rc2 is not None:
        for p in sorted(Path(a.orch_rc2, 'reports').glob('*.md')):
            copy(p, qa / 'rc2' / 'reports' / p.name)
        for name in ['BRIEF.md', 'TASKS.md']:
            if Path(a.orch_rc2, name).exists():
                copy(Path(a.orch_rc2, name), qa / 'rc2' / name)
        for tname in ['R1', 'R2', 'R3', 'V1', 'V2']:
            src = rc2 / tname / 'qa'
            if src.exists():
                shutil.copytree(src, qa / tname, dirs_exist_ok=True)
        for name in ['rc2f_apply_log.csv', 'rc2f_apply_info.json']:      # rc2f: override application log (rc2f_apply.py apply)
            if (rc2 / name).exists():
                copy(rc2 / name, qa / 'rc2f' / name)
        if (rc2 / 'overrides').exists():
            shutil.copytree(rc2 / 'overrides', qa / 'rc2f' / 'overrides', dirs_exist_ok=True)
        if (rc2 / 'recount').exists():
            shutil.copytree(rc2 / 'recount', qa / 'rc2f' / 'recount', dirs_exist_ok=True)
        if (rc2 / 'rc2f_apply_info.json').exists() and a.orch_rc2f and Path(a.orch_rc2f).exists():   # rc2f: orchestration record (brief, board, lead notes, task/review reports, exception registry)
            for name in ['BRIEF.md', 'TASKS.md', 'LEAD_NOTES.md']:
                if Path(a.orch_rc2f, name).exists():
                    copy(Path(a.orch_rc2f, name), qa / 'rc2f' / name)
            for p in sorted(Path(a.orch_rc2f, 'reports').glob('*.md')):
                copy(p, qa / 'rc2f' / 'reports' / p.name)
            for p in sorted(Path(a.orch_rc2f, 'registry').glob('*.csv')):
                copy(p, qa / 'rc2f' / 'registry' / p.name)

    # manifest
    files = {}
    for p in sorted(out.rglob('*')):
        if p.is_file():
            rel = str(p.relative_to(out))
            e = {'bytes': p.stat().st_size, 'sha256': sha256(p)}
            if p.suffix == '.parquet':
                e['rows'] = parquet_rows(p)
            files[rel] = e
    bd = pq.ParquetFile(out / 'base_data.parquet')
    dmax = None; dmin = None
    for rg in range(bd.num_row_groups):
        s = bd.metadata.row_group(rg).column(bd.schema_arrow.get_field_index('date')).statistics
        if s is not None and s.has_min_max:
            dmin = s.min if dmin is None else min(dmin, s.min); dmax = s.max if dmax is None else max(dmax, s.max)
    manifest = {
        'build_id': build_id, 'built_at': dt.datetime.now().isoformat(timespec='seconds'), 'layout': 'cache_v2.1', 'contract': 'orch_v21/BRIEF.md §3 (2026-09-16)' + (' + orch_rc2/BRIEF.md §3 (2026-09-17, cash-style returns, corporate-action ledger, aux instruments)' if rc2 is not None else '') + (' + orch_rc2f/BRIEF.md (2026-09-19, fixed-snapshot exception registry: source-level overrides, chainable-only lineage links, WI link point)' if rc2 is not None and (rc2 / 'rc2f_apply_info.json').exists() else ''),
        'asOfDate': str(pd.Timestamp(dmax).date()) if dmax is not None else None, 'firstDate': str(pd.Timestamp(dmin).date()) if dmin is not None else None,
        'conventions': {
            'prices': 'raw as-quoted USD = ciqpriceequity.price x adjustmentfactor; volume = volume / adjustmentfactor (float64)',
            'factors': 'half-open intervals [validFrom, validTo), validTo NaT = open; splitFactor = cumulative split factor to snapshot; divAdjFactor = vendor dividend+spinoff factor',
            'split_adj': 'raw / splitFactor', 'div_adj': 'raw * divAdjFactor / splitFactor',
            'returns_total': ('vendor-factor total return: adj_t/adj_prev - 1 over observed rows, adj = close/splitFactor*divAdjFactor; rows after lifecycle.lastTradeDate dropped' if a.returns_legacy else
                              'cash-style holding total return v2 (rc2 section 3-2): ret = (shares*close_t + cash + child)/close_prev - 1 over observed rows from the corporate-action ledger '
                              '(events/ca_ledger.parquet; sha256/rows in steps.calendar.returns_rebuilt); retFlag 0 price_only/1 cash/2 distribution/3 estimate/4 unresolved; '
                              + ('lineage-linked first rows use the old id last close + old-id gap events (lineage chainGapTradingDays <= 5); ' if a.legacy_links else 'lineage links (rc2f): chainable == True rows only (chainGapTradingDays <= 5); prev = old id last close (lifecycle-capped) + old-id gap events; when the old id still trades after the new id first print the link row is the first new-id row after the old id last trade (earlier rows = own price returns, first row NaN); ') + 'ledger rows sourced from corporate_actions '
                              '(merger/PVT settlement) are not applied, so settlement days carry the price return only (engine events file settles them); '
                              'rows after lifecycle.lastTradeDate dropped' + (f'; LEDGER STALE after {a.ledger_stale_after}: later rows retFlag 4' if a.ledger_stale_after else '')),
            'qualityFlag': {'0': 'confirmed', '1': 'provisional', '2': 'mi_mismatch (>0.1% vs miprice, value from ciq)', '3': 'ciq_null_close', '4': 'post_lifecycle_tail (date > lastTradeDate; not tradable)',
                            '8': 'price_override (rc2f manual unit correction, qa/rc2f/rc2f_apply_log.csv; post-lifecycle rows keep 4)',
                            '16': 'supplemented (rc2f extend_prices: row added from xfeed ciqpriceequity (source 1) or miprice (source 2, close/volume only); post-lifecycle rows keep 4)',
                            '32': 'price_override_estimate (rc2f price correction marked isEstimate: returns rows on these prices carry retFlag >= 3, ledger split rows corroborated with them are estimated)',
                            '64': 'value override: row created from a second source (rc2f value_overrides)'},
            'lifecycle': 'lastTradeDate = min(ciqLastCloseDate, miLastCloseDate, eventEndDate); status active/confirmed/provisional',
            'calendar': 'calendar.parquet: trading day = nClose >= 20% of centered 41-day rolling median; rows on other dates removed from base_data/returns (qa/off_calendar_rows.csv)',
            'proto_tables': 'dividends/ and distributions/ copied from cache_v2_cash_1d (2026-09-14); *CacheUnit columns there refer to the prototype split-adjusted basis, not raw',
            'ca_ledger': ('events/ca_ledger.parquet = raw-unit corporate-action ledger (rc2 BRIEF section 3-1): one row per (parent id, exDate, leg); cash pre-tax USD per old share, '
                          'parentFactor, child id/ratio, childValuePerOldShare (P10 priority: child ex-date close > when-issued > event value > vendor implied, isEstimate), '
                          'status ok/estimated/unresolved (unresolved is a visible flag, never a hidden drop), repairs in events/ca_ledger_repairs.csv; '
                          'stitched_copy rows (exportExclude) duplicate the keeper event onto a successor id whose history was appended; engine/ca_ledger_cache.parquet = '
                          'split_adj cache-unit export for the C++ engine (cashPerShareCache = cash / sf_parent(prev day), childRatioCache = ratio x sf_child(exDate) / sf_parent(prev day), '
                          'merger/rename/relist and events-duplicate legs excluded)') if rc2 is not None else 'absent',
            'aux_instruments': ('master/aux_instruments.parquet: successor/child securities collected only to value settlements and distributions; present in base_data/factors/lifecycle (status aux) '
                                'and excluded from tradable via master/tradable_exclusions.parquet reason aux_instrument; never universe members') if rc2 is not None else 'absent',
            'retFlag_extensions': ('retFlag 3 also marks lineage-link overlap and new-id legs with a share-basis conversion; retFlag 4 also marks ratio conflicts between old/new id events, '
                                   'implausible linked first returns (|ret|>0.5) and rows after a stale ledger cut') if rc2 is not None else 'absent',
            'adjustment_factor_daily': 'factors/adjustment_factor_daily.parquet = per-row vendor adjustmentfactor and vendor split-adjusted close (double); verification/reconciliation side table, derivable from factors/split.parquet (G-SPLIT identity)',
        },
        'steps': report['steps'], 'files': files,
    }
    (out / 'manifest.json').write_text(json.dumps(manifest, indent=1, default=str))
    readme = f"""# cache_v2.1_cash_1d — build {build_id}

raw(as-quoted) long prices + interval factors + vendor-factor total return + history master. Contract: qa/BRIEF.md §3.
Loader: longshort_platform/cpp-engine/tools/data/cache_v21/loader.py (python reference; `loader.py info --root <this dir> --master-root <this dir>/master`).
asOfDate {manifest['asOfDate']} (last complete trading day). Rows after lifecycle.lastTradeDate: base_data qualityFlag=4 (kept), returns_total dropped.
Do not compute holding returns from close ratios (splits/dividends): use derived/returns_total.parquet.
See qa/reports/*.md for build reports and gates.

KNOWN ISSUES (rc1 independent audit qa/T7/AUDIT.md): vendor divAdjFactor defects (PBCT 2000-07 x2.98, TYC old line 314828302
missing spin-off legs, 55 lines with dividend-factor scale errors) - list in qa/T7/returns_known_issues.csv; 97 vendor split-factor
artifacts (OTC) in qa/T2/factor_artifacts.csv; 3 medium-confidence lineage pairs tradable on the same day (never both in a universe).
""" + ("""
rc2 (2026-09-17): returns_total is now the CASH-STYLE holding return built from events/ca_ledger.parquet (pre-tax, every dividend and
distribution day, no reinvestment; vendor divAdjFactor kept in factors/dividend.parquet for cross-checks only). The vendor defects above no
longer affect returns_total; remaining unresolved days carry retFlag 4 (qa/R2, qa/V2/qa/rc2/known_issues_v2.csv). The C++ engine books
dividends and spin-off children from engine/ca_ledger_cache.parquet (docs/corporate_actions_engine_2026-09-17.md); do not add returns_total
on top of the engine P&L (double counting). Auxiliary instruments (master/aux_instruments.parquet) exist only to value settlements/distributions.
""" if rc2 is not None else '')
    (out / 'README.md').write_text(readme)
    log(f'manifest {len(files)} files, asOf {manifest["asOfDate"]}')

    if a.remote:
        sync_remote(out, Path(a.remote), build_id, files)
    print(json.dumps({k: v for k, v in manifest.items() if k != 'files'}, indent=1, default=str))


if __name__ == '__main__':
    main()
