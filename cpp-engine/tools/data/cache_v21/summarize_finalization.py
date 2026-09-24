#!/home/jgo/usr/venv-3.11/bin/python
"""T3 agent-observe — summary.csv(+zips.csv, loadstate.csv) → 시각대별 표(markdown).

사용:  summarize_finalization.py [--dir DIR] [--out summary_tables.md] [--rel T0 T-1]
표:
  A. 커버리지: 마감 후 경과시간(h) 버킷 × 테이블 → 표본 행 비율(alive 기준)·종가 notnull 비율·전체 행 수 (날짜별 최소/평균)
  B. 변경: 날짜(D) × 테이블 → 마지막으로 변경(셀·새 행·사라진 행)이 관측된 마감 후 경과시간, 총 변경 셀 수, 컬럼별 변경 셀 수
  C. 시각표: 관측 KST 시각(HH:05) × 대상 날짜 → 표본 행 수 / 변경 건수 (T0·T-1 각각)
  D. 일치율: 경과시간 버킷 × 상대차 기준(1e-6/1e-4/1e-3) → 일치율(표본 both-notnull 기준)
  E. 로더 도착표: zips.csv → 파일 패밀리·순번별 다운로드 KST/ET 시각(일별) ; loadstate.csv → 테이블별 마지막 적재 zip
  F. 컷오프 제안 양식(자동 채움): T-1 변경 0이 되는 최소 경과시간(전 날짜 max), 두 원천 도착 순서, 잠정치 비율
"""
import os, sys, argparse, re
import numpy as np, pandas as pd
from zoneinfo import ZoneInfo

D_DEFAULT = '/tmp/claude-1008/-home-jgo/3f2abe1d-a2a7-4017-9ac1-2c7cb961e311/scratchpad/stage_v21/T3/qa/finalization'
KST = ZoneInfo('Asia/Seoul'); ET = ZoneInfo('America/New_York')


def md(df, floatfmt='{:.3g}'):
    df = df.copy()
    for c in df.columns:
        if df[c].dtype.kind == 'f':
            df[c] = df[c].map(lambda v: '' if pd.isna(v) else floatfmt.format(v))
    cols = [str(c) for c in df.columns]
    lines = ['| ' + ' | '.join(cols) + ' |', '|' + '---|' * len(cols)]
    for _, r in df.iterrows():
        lines.append('| ' + ' | '.join('' if (isinstance(v, float) and np.isnan(v)) else str(v) for v in r.values) + ' |')
    return '\n'.join(lines)


def load(d):
    s = pd.read_csv(f'{d}/summary.csv')
    s['obs_kst'] = pd.to_datetime(s['obs_kst']); s['obs_et'] = pd.to_datetime(s['obs_et']); s['date'] = pd.to_datetime(s['date'])
    # 관측마다 같은 (obs_kst, table, date) 가 두 번 들어간 경우(수동 --once 중복) 마지막 것만
    s = s.drop_duplicates(['obs_kst', 'table', 'date'], keep='last')
    s['alive'] = s['sample_n'] - s.groupby('date')['sample_missing'].transform('min')  # 날짜별 최대 관측 행 기준 alive 추정
    s['cov_rows'] = s['sample_rows'] / s['alive']; s['cov_close'] = s['sample_close_notnull'] / s['alive']
    s['n_change'] = s[['changed_cells', 'new_rows', 'gone_rows']].fillna(0).sum(axis=1)
    s['h_bucket'] = np.floor(s['hours_since_close']).astype(int)
    s['kst_hh'] = s['obs_kst'].dt.strftime('%H:%M')
    s['et_hh'] = s['obs_et'].dt.strftime('%H:%M')
    return s


def table_A(s, rel_keep):
    t = s[s['rel'].isin(rel_keep) & (s['h_bucket'] >= -8)]
    g = t.groupby(['table', 'h_bucket'])
    out = pd.DataFrame({'n_obs': g.size(), 'n_dates': g['date'].nunique(),
                        'cov_rows_min': g['cov_rows'].min(), 'cov_rows_mean': g['cov_rows'].mean(),
                        'cov_close_min': g['cov_close'].min(), 'full_rows_mean': g['full_rows'].mean(), 'full_close_mean': g['full_close_notnull'].mean(),
                        'changes_sum': g['n_change'].sum()}).reset_index()
    return out


def table_B(s):
    rows = []
    for (tb, d), g in s.sort_values('obs_kst').groupby(['table', 'date']):
        g2 = g[g['prev_obs'].notna() & (g['prev_obs'] != '')]
        ch = g2[g2['n_change'] > 0]
        first = g[g['sample_rows'] > 0]['hours_since_close'].min()
        g_first = g.iloc[0]   # 이 날짜의 첫 관측
        censored = bool(g_first['sample_rows'] > 0 and g_first['obs_kst'] == s['obs_kst'].min())  # 루프 시작 전에 이미 있던 행 → 진짜 도착 시각은 h_first 이하
        full99 = g[g['cov_rows'] >= 0.99]['hours_since_close'].min()
        r = dict(table=tb, date=d.strftime('%Y-%m-%d'), n_obs=len(g), h_first=round(first, 1) if pd.notna(first) else np.nan, first_censored=censored,
                 h_cov99=round(full99, 1) if pd.notna(full99) else np.nan,
                 h_last_change=round(ch['hours_since_close'].max(), 1) if len(ch) else np.nan,
                 h_last_obs=round(g['hours_since_close'].max(), 1),
                 changes_total=int(g2['n_change'].sum()), changed_cells=int(g2['changed_cells'].fillna(0).sum()),
                 new_rows=int(g2['new_rows'].fillna(0).sum()), gone_rows=int(g2['gone_rows'].fillna(0).sum()))
        for c in ['chg_priceclose', 'chg_priceopen', 'chg_pricehigh', 'chg_pricelow', 'chg_volume', 'chg_vwap', 'chg_adjustmentfactor', 'chg_pacvertofeedpop', 'chg_sourceidentifier']:
            if c in g2.columns: r[c] = int(g2[c].fillna(0).sum())
        rows.append(r)
    return pd.DataFrame(rows)


def table_C(s, rel, value):
    t = s[s['rel'] == rel]
    p = t.pivot_table(index=['table', 'kst_hh'], columns=t['date'].dt.strftime('%m-%d'), values=value, aggfunc='last')
    return p.reset_index()


def table_D(s):
    t = s[s['rel'].isin(['T0', 'T-1', 'T-2']) & (s['table'] == 'ciq') & (s['match_n_both'] > 0)]
    g = t.groupby('h_bucket')
    out = pd.DataFrame({'n_obs': g.size(), 'n_both_mean': g['match_n_both'].mean(),
                        'r_1e6': g['match_1e6'].sum() / g['match_n_both'].sum(), 'r_1e4': g['match_1e4'].sum() / g['match_n_both'].sum(),
                        'r_1e3': g['match_1e3'].sum() / g['match_n_both'].sum(), 'maxrel': g['match_maxrel'].max(),
                        'ciq_only_mean': g['ciq_close_only'].mean(), 'mi_only_mean': g['mi_close_only'].mean()}).reset_index()
    return out


def table_E(d):
    out = []
    zp = f'{d}/zips.csv'
    if os.path.exists(zp):
        z = pd.read_csv(zp); z['dl'] = pd.to_datetime(z['lastdownloadosdatetime'])
        z['family'] = z['actualzipfilename'].str.replace(r'\d{8,14}-\d+\.zip$', '', regex=True)
        z['seq'] = z['actualzipfilename'].str.extract(r'-(\d+)\.zip$')[0].astype(int)
        z['dl_et'] = z['dl'].dt.tz_localize(KST).dt.tz_convert(ET).dt.tz_localize(None)
        ciq = z[z['family'] == 'PriceEquityRecentHistoryIntradayChange'].copy()
        ciq['label_date'] = ciq['actualzipfilename'].str.extract(r'Change(\d{8})-')[0]
        ciq['dl_kst'] = ciq['dl'].dt.strftime('%m-%d %H:%M'); ciq['dl_et_s'] = ciq['dl_et'].dt.strftime('%m-%d %H:%M')
        ciq['kb'] = (ciq['zipfilesize'] / 1024).round(0).astype(int)
        p = ciq.pivot_table(index='label_date', columns='seq', values='dl_kst', aggfunc='last')
        p.columns = [f'seq{c}_dlKST' for c in p.columns]
        pe = ciq.pivot_table(index='label_date', columns='seq', values='dl_et_s', aggfunc='last'); pe.columns = [f'seq{c}_dlET' for c in pe.columns]
        pk = ciq.pivot_table(index='label_date', columns='seq', values='kb', aggfunc='last'); pk.columns = [f'seq{c}_KB' for c in pk.columns]
        out.append(('E-1. ciq PriceEquityRecentHistoryIntradayChange 도착(다운로드) 시각 — 파일 라벨일(미국 날짜) × 순번', pd.concat([p, pe, pk], axis=1).reset_index()))
        mi = z[z['family'] == 'MIPriceRecentHistChange'].copy()
        mi['kst_h'] = mi['dl'].dt.hour; mi['et_h'] = mi['dl_et'].dt.hour; mi['day'] = mi['dl'].dt.date
        ndays = mi['day'].nunique()
        hb = mi.groupby(['kst_h', 'et_h']).agg(n_files=('zipfilesize', 'size'), kb_total=('zipfilesize', lambda v: int(v.sum() / 1024)),
                                              kb_max=('zipfilesize', lambda v: int(v.max() / 1024))).reset_index()
        hb['kb_per_day'] = (hb['kb_total'] / max(ndays, 1)).round(0).astype(int)
        hb = hb.sort_values('et_h')
        out.append((f'E-2. MIPriceRecentHistChange 시간대(hour-of-day)별 도착 — {ndays}일 합계 (15분 간격 파일; 용량이 큰 시간대 = 종가 일괄 도착)', hb))
    lp = f'{d}/loadstate.csv'
    if os.path.exists(lp):
        l = pd.read_csv(lp)
        l = l[l['tablename'].isin(['ciqPriceEquity', 'MIPrice']) & l['lastzip'].fillna('').str.contains('RecentHist')]
        l['load_end_kst'] = (pd.to_datetime(l['enddatetime']).dt.tz_localize('UTC').dt.tz_convert(KST)).dt.strftime('%m-%d %H:%M')
        l = l[['obs_kst', 'tablename', 'load_end_kst', 'lastzip', 'lastzip_dl_kst']].drop_duplicates(['tablename', 'lastzip'])
        out.append(('E-3. 관측 시점의 테이블 최종 적재 상태(xfu_loadsubtablestate; 적재 종료 UTC→KST, 마지막 zip·다운로드 KST)', l))
    return out


def table_F(s):
    """컷오프 제안 양식. T-1·T0 각 테이블: 변경 0 이후 유지되는 최소 경과시간(전 날짜 max), 도착 순서, 잠정치 비율."""
    lines = []
    B = table_B(s)
    for tb in ['ciq', 'mi']:
        b = B[B['table'] == tb]
        b_done = b[b['h_last_obs'] >= 24]  # 마감 후 24h 이상 관측된 날짜만
        if len(b_done):
            unc = b_done[~b_done['first_censored']]
            # 날짜별 '마지막 변경' = 변경이 관측됐으면 그 시각, 변경이 전혀 없었으면 비검열 날짜만 첫 도착 시각(=도착 즉시 확정), 검열 날짜는 판정 제외
            hlc = b_done['h_last_change'].where(b_done['h_last_change'].notna(), b_done['h_first'].where(~b_done['first_censored'])).dropna()
            hf = f'첫 행 도착 h_first 중위 {unc["h_first"].median():.1f}h(최대 {unc["h_first"].max():.1f}h, 비검열 {len(unc)}일)' if len(unc) else '첫 행 도착: 비검열 날짜 없음'
            hl = (f'마지막 변경 h_last_change 최대 {hlc.max():.1f}h(중위 {hlc.median():.1f}h, 판정 가능 {len(hlc)}일; 변경 관측 0인 날짜 {int(b_done["h_last_change"].isna().sum())}개) '
                  f'→ confirmed 컷오프 후보 = 마감 + {np.ceil(hlc.max()):.0f}h') if len(hlc) else '마지막 변경: 판정 가능 날짜 없음(검열 날짜만 있음)'
            lines.append(f'- {tb}: 관측 완료 날짜 {len(b_done)}개 | {hf} | {hl}')
        else:
            lines.append(f'- {tb}: 마감 후 24h 이상 관측된 날짜 없음(아직 판정 불가)')
    # 도착 순서: 날짜별 h_first 비교
    piv = B[~B['first_censored']].pivot_table(index='date', columns='table', values='h_first')
    if {'ciq', 'mi'} <= set(piv.columns):
        d = (piv['ciq'] - piv['mi']).dropna()
        if len(d):
            lines.append(f'- 도착 순서(h_first ciq − mi, 날짜 {len(d)}개): 중위 {d.median():+.1f}h, ciq 먼저 {int((d < 0).sum())}일 / 동시 {int((d == 0).sum())}일 / mi 먼저 {int((d > 0).sum())}일')
    # 잠정치 비율: 첫 도착 관측의 값 중 이후 바뀐 셀 비율 ≈ changed_cells / (rows × 값 컬럼 수)
    for tb, ncol in [('ciq', 8), ('mi', 6)]:
        b = B[(B['table'] == tb) & (B['h_last_obs'] >= 24)]
        t = s[(s['table'] == tb) & s['rel'].isin(['T0', 'T-1', 'T-2'])]
        if len(b) and len(t):
            rows_mean = t.groupby('date')['sample_rows'].max().reindex(pd.to_datetime(b['date'])).mean()
            lines.append(f'- {tb} 잠정치 비율[추정]: 이후 바뀐 셀 {int(b["changed_cells"].sum())} / (행 {rows_mean:.0f} × {ncol}컬럼 × {len(b)}일) = {b["changed_cells"].sum() / max(rows_mean * ncol * len(b), 1):.2%}')
    return '\n'.join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir', default=D_DEFAULT); ap.add_argument('--out', default=None); ap.add_argument('--rel', nargs='+', default=['T0', 'T-1'])
    a = ap.parse_args()
    s = load(a.dir)
    parts = [f'# finalization 관측 요약 (관측 {s["obs_kst"].nunique()}회, {s["obs_kst"].min():%Y-%m-%d %H:%M} ~ {s["obs_kst"].max():%Y-%m-%d %H:%M} KST, 대상 날짜 {s["date"].nunique()}개)\n']
    parts.append(f'## A. 커버리지 — 마감 후 경과시간 버킷(h) × 테이블 (rel ∈ {a.rel}; cov = 표본 행/alive)\n' + md(table_A(s, a.rel)))
    parts.append('## B. 날짜별 도착·변경 — h_first 첫 행 관측 경과시간, h_cov99 커버리지 99% 도달, h_last_change 마지막 변경 관측 경과시간\n' + md(table_B(s)))
    for rel in a.rel:
        parts.append(f'## C-{rel}. 관측 KST 시각 × 날짜 → 표본 행 수\n' + md(table_C(s, rel, 'sample_rows')))
        parts.append(f'## C-{rel}. 관측 KST 시각 × 날짜 → 변경 건수(셀+새 행+사라진 행)\n' + md(table_C(s, rel, 'n_change')))
    parts.append('## D. 두 원천 종가 일치율 — 경과시간 버킷(표본, both-notnull 기준; ciq priceclose×adjustmentfactor vs mi priceclose)\n' + md(table_D(s), '{:.6f}'))
    for title, df in table_E(a.dir):
        parts.append(f'## {title}\n' + md(df))
    parts.append('## F. 컷오프 제안 양식(자동 채움)\n' + table_F(s))
    text = '\n\n'.join(parts) + '\n'
    out = a.out or f'{a.dir}/summary_tables.md'
    open(out, 'w').write(text)
    print(text)
    print(f'-> {out}', file=sys.stderr)


if __name__ == '__main__':
    main()
