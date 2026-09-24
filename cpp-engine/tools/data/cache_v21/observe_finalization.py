#!/home/jgo/usr/venv-3.11/bin/python
"""T3 agent-observe — xfeed 가격 테이블(ciqpriceequity·miprice)의 T0/T-1/T-2 도착·확정 시각 관측 CLI.

한 번 실행(--once)마다:
  1. 관측 시각(KST·ET)과 NYSE 달력 기준 대상 거래일 T0(정규장 마감 16:00 ET가 지난 최근 거래일), T-1, T-2 결정
  2. 표본(Top500 프로토타입 마지막 60거래일 멤버 ∪) → 두 테이블 값 단위 조회(date >= T-2, id 리스트 = any(%s))
  3. 전체(프로토타입 master 22,871 id) → 청크(≤1000) × 2 테이블: 날짜별 행 수·종가 notnull 수만 집계
  4. 로더 제어(xfl_control): 가격 zip 도착 시각(xfu_actualzipfile)·테이블 최종 적재 상태(xfu_loadsubtablestate) 스냅샷
  5. 직전 관측 parquet과 대조: 값이 바뀐 셀, 새 행, 사라진 행 (표본, 날짜별)
  6. 두 원천 종가 일치율(ciq priceclose×adjustmentfactor vs mi priceclose, 상대차 1e-6/1e-4/1e-3)
  7. 파일: obs_YYYYMMDD_HHMM.parquet(값), summary.csv(append), zips.csv(append, dedupe), loadstate.csv(append)

DB 규칙: 읽기 전용 세션, statement_timeout 5분, 큰 테이블은 id 리스트 + 날짜 조건으로만 조회(전체 스캔 금지).
스레드: pandas/numpy만 사용, OMP/MKL 스레드 2로 제한.

NYSE 달력: venv에 pandas_market_calendars 가 없어(2026-09-16 확인) 2026년 NYSE 휴장일·조기폐장일을 하드코딩함.
  휴장 2026: 01-01 New Year, 01-19 MLK, 02-16 Washington, 04-03 Good Friday, 05-25 Memorial, 06-19 Juneteenth,
            07-03 Independence(관측일, 7/4 토), 09-07 Labor, 11-26 Thanksgiving, 12-25 Christmas
  조기폐장(13:00 ET) 2026: 11-27, 12-24
  DST는 zoneinfo America/New_York 로 처리(정규장 마감 = 현지 16:00).
"""
import os
os.environ.setdefault('OMP_NUM_THREADS', '2'); os.environ.setdefault('OPENBLAS_NUM_THREADS', '2'); os.environ.setdefault('MKL_NUM_THREADS', '2')
import sys, argparse, glob, json, time, datetime as dt
from zoneinfo import ZoneInfo
import numpy as np, pandas as pd

KST = ZoneInfo('Asia/Seoul'); ET = ZoneInfo('America/New_York')
STAGE = '/tmp/claude-1008/-home-jgo/3f2abe1d-a2a7-4017-9ac1-2c7cb961e311/scratchpad/stage_v21/T3'
OUT_DIR = f'{STAGE}/qa/finalization'
PROTO = '/home/jgo/61/data2/temp_stock/cache_v2_cash_1d'
TOP500 = f'{PROTO}/TopUniverseV2/Top500.parquet'
MASTER = f'{PROTO}/Master/master.parquet'
SAMPLE_LOOKBACK = 60          # Top500 마지막 60거래일 중 한 번이라도 True
CHUNK = 1000                  # 큰 테이블 id 청크 상한(BRIEF §1)
STMT_TIMEOUT_MS = 300_000     # 쿼리 하나당 5분

# ---------------------------------------------------------------- NYSE calendar (hardcoded; see module docstring)
NYSE_HOLIDAYS = {
    # 2025 (T-2 lookback 안전용)
    '2025-01-01', '2025-01-09', '2025-01-20', '2025-02-17', '2025-04-18', '2025-05-26', '2025-06-19', '2025-07-04',
    '2025-09-01', '2025-11-27', '2025-12-25',
    # 2026
    '2026-01-01', '2026-01-19', '2026-02-16', '2026-04-03', '2026-05-25', '2026-06-19', '2026-07-03',
    '2026-09-07', '2026-11-26', '2026-12-25',
    # 2027 (루프가 넘어갈 경우 안전용)
    '2027-01-01', '2027-01-18', '2027-02-15', '2027-03-26', '2027-05-31', '2027-06-18', '2027-07-05',
    '2027-09-06', '2027-11-25', '2027-12-24',
}
NYSE_EARLY_CLOSE = {'2025-07-03', '2025-11-28', '2025-12-24', '2026-11-27', '2026-12-24', '2027-11-26'}
NYSE_HOLIDAYS = {dt.date.fromisoformat(s) for s in NYSE_HOLIDAYS}
NYSE_EARLY_CLOSE = {dt.date.fromisoformat(s) for s in NYSE_EARLY_CLOSE}


def is_trading_day(d: dt.date) -> bool:
    return d.weekday() < 5 and d not in NYSE_HOLIDAYS


def close_dt(d: dt.date) -> dt.datetime:
    """정규장 마감 시각(ET, tz-aware). 조기폐장일은 13:00."""
    h = 13 if d in NYSE_EARLY_CLOSE else 16
    return dt.datetime(d.year, d.month, d.day, h, 0, tzinfo=ET)


def prev_trading_day(d: dt.date) -> dt.date:
    d -= dt.timedelta(days=1)
    while not is_trading_day(d):
        d -= dt.timedelta(days=1)
    return d


def next_trading_day(d: dt.date) -> dt.date:
    d += dt.timedelta(days=1)
    while not is_trading_day(d):
        d += dt.timedelta(days=1)
    return d


def target_days(now_et: dt.datetime):
    """T0 = 정규장 마감이 now_et 이전인 최근 거래일. 반환 (T0, T-1, T-2, T+1=다음 거래일)."""
    d = now_et.date()
    while not (is_trading_day(d) and close_dt(d) <= now_et):
        d -= dt.timedelta(days=1)
    t0 = d; t1 = prev_trading_day(t0); t2 = prev_trading_day(t1); tn = next_trading_day(t0)
    return t0, t1, t2, tn


def nth_trading_day_after(d: dt.date, n: int) -> dt.date:
    for _ in range(n):
        d = next_trading_day(d)
    return d


# ---------------------------------------------------------------- DB
def connect():
    import psycopg2
    params = dict(host='localhost', database='xfeed', user='loader', port='5432',
                  connect_timeout=30,
                  options=f'-c default_transaction_read_only=on -c statement_timeout={STMT_TIMEOUT_MS}')
    if os.environ.get("XFEED_DB_PASSWORD"):
        params["password"] = os.environ["XFEED_DB_PASSWORD"]
    c = psycopg2.connect(**params)
    c.set_session(readonly=True)
    return c


def q(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall(); cols = [d[0] for d in cur.description]
    conn.rollback()  # 읽기 전용 트랜잭션 종료(스냅샷 갱신)
    return pd.DataFrame(rows, columns=cols)


CIQ_COLS = ['priceclose', 'priceopen', 'pricehigh', 'pricelow', 'volume', 'vwap', 'adjustmentfactor', 'pacvertofeedpop']
MI_COLS = ['priceclose', 'priceopen', 'pricehigh', 'pricelow', 'volume', 'sourceidentifier']
VALUE_COLS = ['priceclose', 'priceopen', 'pricehigh', 'pricelow', 'volume', 'vwap', 'adjustmentfactor', 'pacvertofeedpop', 'sourceidentifier']
TABLES = {'ciq': ('xfl_target.ciqpriceequity', 'pricingdate', CIQ_COLS),
          'mi': ('xfl_target.miprice', 'pricedate', MI_COLS)}


def chunks(ids, n=CHUNK):
    ids = list(map(int, ids))
    for i in range(0, len(ids), n):
        yield ids[i:i + n]


def fetch_sample(conn, table, ids, from_date):
    tbl, dcol, cols = TABLES[table]
    parts = []
    for ch in chunks(ids):
        sql = f"select tradingitemid, {dcol} as date, {', '.join(cols)} from {tbl} where tradingitemid = any(%s) and {dcol} >= %s"
        parts.append(q(conn, sql, (ch, from_date)))
    df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=['tradingitemid', 'date'] + cols)
    df['table'] = table
    for c in VALUE_COLS:
        if c not in df.columns:
            df[c] = np.nan
    df['tradingitemid'] = df['tradingitemid'].astype('int64')
    df['date'] = pd.to_datetime(df['date']).dt.normalize()
    for c in VALUE_COLS:
        df[c] = pd.to_numeric(df[c], errors='coerce').astype('float64')
    return df[['table', 'tradingitemid', 'date'] + VALUE_COLS]


def fetch_full_counts(conn, table, ids, from_date):
    """전체 id(청크)별 날짜 집계: 행 수, 종가 notnull 수. 반환 DataFrame(date, rows, close_notnull, n_chunks)."""
    tbl, dcol, _ = TABLES[table]
    parts = []; n = 0
    for ch in chunks(ids):
        sql = f"select {dcol} as date, count(*) as rows, count(priceclose) as close_notnull from {tbl} where tradingitemid = any(%s) and {dcol} >= %s group by 1"
        parts.append(q(conn, sql, (ch, from_date))); n += 1
    df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=['date', 'rows', 'close_notnull'])
    if len(df):
        df['date'] = pd.to_datetime(df['date']).dt.normalize()
        df = df.groupby('date', as_index=False)[['rows', 'close_notnull']].sum()
    df['n_chunks'] = n
    return df


ZIP_FAMILIES = "^(PriceEquity(RecentHistory|1990s|ExtendedHistory)IntradayChange|MIPrice(RecentHist|ExtdHist)Change|MIAdjPrice(RecentHist|ExtdHist)Change)"


def fetch_loader_control(conn, since_kst: dt.datetime):
    """xfl_control 스냅샷(작은 테이블). zips: 가격 zip 도착(다운로드 KST), loadstate: 테이블별 최종 적재(UTC)."""
    zips = q(conn, "select actualzipfileid, actualzipfilename, zipfiledate, lastdownloadosdatetime, zipfilesize "
                   "from xfl_control.xfu_actualzipfile where actualzipfilename ~ %s and lastdownloadosdatetime >= %s "
                   "order by lastdownloadosdatetime", (ZIP_FAMILIES, since_kst.replace(tzinfo=None)))
    state = q(conn, "select t.tablename, s.loadsubid, s.startdatetime, s.enddatetime, s.lastresulttypeid, s.loadsubtabletcount, "
                    "z.actualzipfilename as lastzip, z.lastdownloadosdatetime as lastzip_dl_kst "
                    "from xfl_control.xfu_loadsubtablestate s join xfl_control.xfc_tableinst ti using(tableinstid) "
                    "join xfl_control.xfc_table t using(tableid) left join xfl_control.xfu_actualzipfile z on z.actualzipfileid = s.lastactualzipfileid "
                    "where t.tablename in ('ciqPriceEquity','MIPrice','MIAdjPrice','ciqPriceEquityDivAdjFactor') and s.enddatetime is not null")
    return zips, state


# ---------------------------------------------------------------- sample / master ids
def load_sample_ids(out_dir):
    """Top500 프로토타입 마지막 60거래일 멤버 합집합. 첫 실행에 out_dir/sample_ids.csv 로 고정(10일간 표본 불변)."""
    cache = f'{out_dir}/sample_ids.csv'
    if os.path.exists(cache):
        return pd.read_csv(cache)['tradingItemId'].astype('int64').tolist()
    panel = pd.read_parquet(TOP500)
    tail = panel.iloc[-SAMPLE_LOOKBACK:]
    ids = [int(c) for c in tail.columns[tail.any(axis=0)]]
    m = pd.read_parquet(MASTER, columns=['tradingItemId', 'symbol', 'lastPriceDate'])
    sm = m[m.tradingItemId.isin(ids)].copy()
    sm['panelLastDate'] = panel.index[-1]
    sm.to_csv(cache, index=False)
    return sorted(ids)


def load_master_ids(out_dir):
    """프로토타입 master 22,871 id. 첫 실행에 out_dir/master_ids.csv 로 고정(sshfs 마운트 장애에도 관측 지속)."""
    cache = f'{out_dir}/master_ids.csv'
    if os.path.exists(cache):
        m = pd.read_csv(cache)
    else:
        m = pd.read_parquet(MASTER, columns=['tradingItemId', 'lastPriceDate'])
        m['alive'] = m['lastPriceDate'] >= m['lastPriceDate'].max()
        m[['tradingItemId', 'alive']].to_csv(cache, index=False)
    return m['tradingItemId'].astype('int64').tolist(), int(m['alive'].sum())


# ---------------------------------------------------------------- diff engine
def diff_obs(prev: pd.DataFrame, cur: pd.DataFrame, from_date):
    """직전 관측과 대조. 공통 창(date >= from_date)에서 (table, id, date) 키로 정렬 후
    changed_cells(값이 바뀐 셀; NaN==NaN은 같음), changed_rows, new_rows(지금만 있음), gone_rows(전에만 있음)를 (table, date)별로 집계."""
    key = ['table', 'tradingitemid', 'date']
    p = prev[prev['date'] >= from_date].set_index(key)[VALUE_COLS]
    c = cur[cur['date'] >= from_date].set_index(key)[VALUE_COLS]
    common = p.index.intersection(c.index)
    pv = p.loc[common].to_numpy(dtype='float64'); cv = c.loc[common].to_numpy(dtype='float64')
    neq = ~((pv == cv) | (np.isnan(pv) & np.isnan(cv)))
    cell = pd.DataFrame(neq, index=common, columns=VALUE_COLS)
    per = cell.groupby(level=['table', 'date']).sum()
    per['changed_cells'] = per[VALUE_COLS].sum(axis=1)
    per['changed_rows'] = cell.any(axis=1).groupby(level=['table', 'date']).sum()
    new = c.index.difference(p.index).to_frame(index=False).groupby(['table', 'date']).size().rename('new_rows')
    gone = p.index.difference(c.index).to_frame(index=False).groupby(['table', 'date']).size().rename('gone_rows')
    out = per.join(new, how='outer').join(gone, how='outer').fillna(0).astype('int64').reset_index()
    out = out.rename(columns={v: f'chg_{v}' for v in VALUE_COLS})
    # 바뀐 셀 목록(값 기록용)
    changed_keys = cell.index[cell.any(axis=1)]
    detail = None
    if len(changed_keys):
        detail = pd.concat({'prev': p.loc[changed_keys], 'cur': c.loc[changed_keys]}, axis=1)
        detail.columns = [f'{a}_{b}' for a, b in detail.columns]
        detail = detail.reset_index()
    return out, detail


def match_rates(sample: pd.DataFrame):
    """표본 날짜별 두 원천 종가 일치율. ciq raw close = priceclose × adjustmentfactor vs mi priceclose."""
    ciq = sample[sample['table'] == 'ciq'].set_index(['tradingitemid', 'date'])
    mi = sample[sample['table'] == 'mi'].set_index(['tradingitemid', 'date'])
    a = (ciq['priceclose'] * ciq['adjustmentfactor']).rename('ciq_raw')
    b = mi['priceclose'].rename('mi')
    j = pd.concat([a, b], axis=1)
    both = j.dropna()
    rel = ((both['ciq_raw'] - both['mi']).abs() / both['mi'].abs()).replace([np.inf], np.nan)
    g = both.assign(rel=rel).groupby(level='date')
    out = pd.DataFrame({
        'match_n_both': g.size(),
        'match_1e6': g['rel'].apply(lambda s: int((s <= 1e-6).sum())),
        'match_1e4': g['rel'].apply(lambda s: int((s <= 1e-4).sum())),
        'match_1e3': g['rel'].apply(lambda s: int((s <= 1e-3).sum())),
        'match_maxrel': g['rel'].max(),
    })
    only = j[j.isna().any(axis=1)]
    out['ciq_close_only'] = only[only['mi'].isna() & only['ciq_raw'].notna()].groupby(level='date').size()
    out['mi_close_only'] = only[only['ciq_raw'].isna() & only['mi'].notna()].groupby(level='date').size()
    return out.fillna(0).reset_index()


# ---------------------------------------------------------------- one observation
def observe(out_dir, full=True, now=None, log=print):
    t_start = time.time()
    now_kst = (now or dt.datetime.now(tz=KST)).astimezone(KST); now_et = now_kst.astimezone(ET)
    t0, t1, t2, tn = target_days(now_et)
    rel_of = {pd.Timestamp(t0): 'T0', pd.Timestamp(t1): 'T-1', pd.Timestamp(t2): 'T-2', pd.Timestamp(tn): 'T+1'}
    from_date = t2
    tag = now_kst.strftime('%Y%m%d_%H%M')
    os.makedirs(out_dir, exist_ok=True)
    lock = f'{out_dir}/.lock'
    if os.path.exists(lock) and time.time() - os.path.getmtime(lock) < 1200:
        log(f'[{tag}] another observation is running (lock age {time.time()-os.path.getmtime(lock):.0f}s) — skip'); return None
    open(lock, 'w').write(str(os.getpid()))
    try:
        sample_ids = load_sample_ids(out_dir)
        master_ids, n_alive = load_master_ids(out_dir)
        log(f'[{tag}] obs KST {now_kst:%Y-%m-%d %H:%M} / ET {now_et:%Y-%m-%d %H:%M %Z} | T0={t0} T-1={t1} T-2={t2} T+1={tn} | sample={len(sample_ids)} master={len(master_ids)}')
        conn = connect()
        timings = {}
        # 1) 표본 값 조회
        tt = time.time(); s_ciq = fetch_sample(conn, 'ciq', sample_ids, from_date); timings['sample_ciq'] = time.time() - tt
        tt = time.time(); s_mi = fetch_sample(conn, 'mi', sample_ids, from_date); timings['sample_mi'] = time.time() - tt
        sample = pd.concat([s_ciq, s_mi], ignore_index=True)
        sample['rel'] = sample['date'].map(rel_of).fillna('other')
        sample['obs_kst'] = now_kst.replace(tzinfo=None); sample['obs_et'] = now_et.replace(tzinfo=None)
        sample = sample.sort_values(['table', 'tradingitemid', 'date']).reset_index(drop=True)
        # 2) 전체 집계
        fulls = {}
        if full:
            for tb in ('ciq', 'mi'):
                tt = time.time(); fulls[tb] = fetch_full_counts(conn, tb, master_ids, from_date); timings[f'full_{tb}'] = time.time() - tt
        # 3) 로더 제어 스냅샷
        tt = time.time()
        zips, state = fetch_loader_control(conn, now_kst - dt.timedelta(days=4)); timings['control'] = time.time() - tt
        conn.close()
        # 4) 직전 관측 대조
        prev_files = sorted(f for f in glob.glob(f'{out_dir}/obs_*.parquet') if os.path.basename(f) < f'obs_{tag}.parquet')
        diff = detail = None; prev_tag = ''
        if prev_files:
            prev = pd.read_parquet(prev_files[-1]); prev_tag = os.path.basename(prev_files[-1])[4:17]
            common_from = max(pd.Timestamp(from_date), prev['date'].min())
            diff, detail = diff_obs(prev, sample, common_from)
        # 5) 일치율
        mr = match_rates(sample)
        # 6) 파일 쓰기
        obs_path = f'{out_dir}/obs_{tag}.parquet'
        sample.to_parquet(obs_path, index=False, compression='zstd')
        if detail is not None and len(detail):
            detail['obs_kst'] = now_kst.replace(tzinfo=None); detail['prev_obs'] = prev_tag
            detail.to_parquet(f'{out_dir}/changes_{tag}.parquet', index=False, compression='zstd')
        rows = []
        dates = sorted(set(sample['date']) | {pd.Timestamp(t0), pd.Timestamp(t1), pd.Timestamp(t2), pd.Timestamp(tn)})  # T+1은 행 0이라도 기록(장중 행 유무 판정용)
        for tb in ('ciq', 'mi'):
            st = sample[sample['table'] == tb]
            for d in dates:
                sd = st[st['date'] == d]
                r = dict(obs_kst=now_kst.strftime('%Y-%m-%d %H:%M'), obs_et=now_et.strftime('%Y-%m-%d %H:%M'), table=tb,
                         date=d.strftime('%Y-%m-%d'), rel=rel_of.get(d, 'other'),
                         hours_since_close=round((now_et - close_dt(d.date())).total_seconds() / 3600, 2),
                         sample_n=len(sample_ids), sample_rows=len(sd), sample_close_notnull=int(sd['priceclose'].notna().sum()),
                         sample_missing=len(sample_ids) - len(sd),
                         full_n=len(master_ids), full_n_alive=n_alive, full_rows=np.nan, full_close_notnull=np.nan,
                         prev_obs=prev_tag, changed_cells=np.nan, changed_rows=np.nan, new_rows=np.nan, gone_rows=np.nan,
                         **{f'chg_{v}': np.nan for v in VALUE_COLS})   # 고정 스키마(첫 관측도 같은 컬럼)
                if tb in fulls and len(fulls[tb]):
                    fr = fulls[tb][fulls[tb]['date'] == d]
                    r['full_rows'] = int(fr['rows'].sum()) if len(fr) else 0
                    r['full_close_notnull'] = int(fr['close_notnull'].sum()) if len(fr) else 0
                elif tb in fulls:
                    r['full_rows'] = 0; r['full_close_notnull'] = 0
                if diff is not None:
                    dr = diff[(diff['table'] == tb) & (diff['date'] == d)]
                    for k in ('changed_cells', 'changed_rows', 'new_rows', 'gone_rows'):
                        r[k] = int(dr[k].sum()) if len(dr) else 0
                    for v in VALUE_COLS:
                        r[f'chg_{v}'] = int(dr[f'chg_{v}'].sum()) if len(dr) else 0
                m = mr[mr['date'] == d]
                for k in ('match_n_both', 'match_1e6', 'match_1e4', 'match_1e3', 'match_maxrel', 'ciq_close_only', 'mi_close_only'):
                    r[k] = (m[k].iloc[0] if len(m) else 0)
                r['elapsed_s'] = round(time.time() - t_start, 1)
                rows.append(r)
        summ = pd.DataFrame(rows)
        spath = f'{out_dir}/summary.csv'
        summ.to_csv(spath, mode='a', header=not os.path.exists(spath), index=False)
        # 로더 제어
        if len(zips):
            zpath = f'{out_dir}/zips.csv'
            if os.path.exists(zpath):
                old = pd.read_csv(zpath); zips = zips[~zips['actualzipfileid'].isin(old['actualzipfileid'])]
            if len(zips):
                zips.to_csv(zpath, mode='a', header=not os.path.exists(zpath), index=False)
        if len(state):
            state.insert(0, 'obs_kst', now_kst.strftime('%Y-%m-%d %H:%M'))
            lpath = f'{out_dir}/loadstate.csv'
            state.to_csv(lpath, mode='a', header=not os.path.exists(lpath), index=False)
        # 콘솔 요약
        view = summ[summ['rel'].isin(['T+1', 'T0', 'T-1', 'T-2'])][['table', 'date', 'rel', 'hours_since_close', 'sample_rows', 'sample_close_notnull', 'full_rows', 'full_close_notnull', 'changed_cells', 'new_rows', 'gone_rows', 'match_n_both', 'match_1e4']]
        log(view.to_string(index=False))
        log(f'[{tag}] wrote {obs_path} rows={len(sample)} | timings ' + ', '.join(f'{k}={v:.1f}s' for k, v in timings.items()) + f' | total {time.time()-t_start:.1f}s')
        return obs_path
    finally:
        try: os.remove(lock)
        except OSError: pass


# ---------------------------------------------------------------- self tests (반례로 실패 판정 가능함을 먼저 보임)
def selftest():
    ok = True
    def check(name, cond):
        nonlocal ok; ok &= bool(cond); print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    print('calendar:')
    d = dt.date
    t = target_days(dt.datetime(2026, 9, 4, 17, 0, tzinfo=ET)); check('Fri 09-04 17:00 ET -> T0=09-04, T-1=09-03, T-2=09-02', t[:3] == (d(2026, 9, 4), d(2026, 9, 3), d(2026, 9, 2)))
    t = target_days(dt.datetime(2026, 9, 7, 12, 0, tzinfo=ET)); check('Labor Day 09-07 -> T0=09-04, T+1=09-08', (t[0], t[3]) == (d(2026, 9, 4), d(2026, 9, 8)))
    t = target_days(dt.datetime(2026, 9, 8, 15, 59, tzinfo=ET)); check('Tue 09-08 15:59 ET -> T0=09-04 (마감 전)', t[0] == d(2026, 9, 4))
    t = target_days(dt.datetime(2026, 9, 8, 16, 0, tzinfo=ET)); check('Tue 09-08 16:00 ET -> T0=09-08', t[0] == d(2026, 9, 8))
    t = target_days(dt.datetime(2026, 9, 16, 2, 10, tzinfo=ET)); check('Wed 09-16 02:10 ET -> T0=09-15, T-1=09-14, T-2=09-11', t[:3] == (d(2026, 9, 15), d(2026, 9, 14), d(2026, 9, 11)))
    t = target_days(dt.datetime(2026, 11, 2, 20, 59, tzinfo=ZoneInfo('UTC'))); check('DST end: 11-02 20:59 UTC (=15:59 EST) -> T0=10-30', t[0] == d(2026, 10, 30))
    t = target_days(dt.datetime(2026, 11, 2, 21, 0, tzinfo=ZoneInfo('UTC'))); check('DST end: 11-02 21:00 UTC (=16:00 EST) -> T0=11-02', t[0] == d(2026, 11, 2))
    t = target_days(dt.datetime(2026, 9, 16, 20, 0, tzinfo=ZoneInfo('UTC'))); check('DST on: 09-16 20:00 UTC (=16:00 EDT) -> T0=09-16', t[0] == d(2026, 9, 16))
    t = target_days(dt.datetime(2026, 11, 27, 13, 30, tzinfo=ET)); check('early close 11-27 13:30 ET -> T0=11-27', t[0] == d(2026, 11, 27))
    check('nth_trading_day_after(09-15, 10) = 09-29', nth_trading_day_after(d(2026, 9, 15), 10) == d(2026, 9, 29))
    print('diff engine (반례: 셀 1 변경, 새 행 1, 사라진 행 1, 창 밖 행 무시):')
    base = dict(table='ciq', priceopen=1.0, pricehigh=1.0, pricelow=1.0, volume=10.0, vwap=np.nan, adjustmentfactor=1.0, pacvertofeedpop=561.0, sourceidentifier=np.nan)
    prev = pd.DataFrame([dict(base, tradingitemid=1, date=pd.Timestamp('2026-09-14'), priceclose=10.0),
                         dict(base, tradingitemid=2, date=pd.Timestamp('2026-09-14'), priceclose=20.0),   # gone
                         dict(base, tradingitemid=1, date=pd.Timestamp('2026-09-10'), priceclose=5.0)])   # 창 밖
    cur = pd.DataFrame([dict(base, tradingitemid=1, date=pd.Timestamp('2026-09-14'), priceclose=10.5),    # changed
                        dict(base, tradingitemid=3, date=pd.Timestamp('2026-09-14'), priceclose=30.0),    # new
                        dict(base, tradingitemid=1, date=pd.Timestamp('2026-09-15'), priceclose=11.0)])   # new (T0)
    out, detail = diff_obs(prev, cur, pd.Timestamp('2026-09-11'))
    r14 = out[out['date'] == pd.Timestamp('2026-09-14')].iloc[0]; r15 = out[out['date'] == pd.Timestamp('2026-09-15')].iloc[0]
    check('09-14 changed_cells=1', r14['changed_cells'] == 1); check('09-14 chg_priceclose=1', r14['chg_priceclose'] == 1)
    check('09-14 new_rows=1 gone_rows=1', (r14['new_rows'], r14['gone_rows']) == (1, 1))
    check('09-15 new_rows=1 changed=0', (r15['new_rows'], r15['changed_cells']) == (1, 0))
    check('창 밖(09-10) 행은 gone으로 세지 않음', out['gone_rows'].sum() == 1)
    check('detail 1행 prev 10.0 -> cur 10.5', len(detail) == 1 and detail['prev_priceclose'].iloc[0] == 10.0 and detail['cur_priceclose'].iloc[0] == 10.5)
    same, _ = diff_obs(cur, cur.copy(), pd.Timestamp('2026-09-11')); check('동일 프레임 -> 변경 0 (NaN==NaN)', same[['changed_cells', 'new_rows', 'gone_rows']].sum().sum() == 0)
    print('match rates (반례: 1e-5 차이는 1e-6 실패·1e-4 통과, adjustmentfactor 2배 반영):')
    s = pd.DataFrame([dict(table='ciq', tradingitemid=1, date=pd.Timestamp('2026-09-15'), priceclose=50.0, adjustmentfactor=2.0),
                      dict(table='mi', tradingitemid=1, date=pd.Timestamp('2026-09-15'), priceclose=100.001),
                      dict(table='ciq', tradingitemid=2, date=pd.Timestamp('2026-09-15'), priceclose=10.0, adjustmentfactor=1.0),
                      dict(table='mi', tradingitemid=2, date=pd.Timestamp('2026-09-15'), priceclose=10.0),
                      dict(table='ciq', tradingitemid=3, date=pd.Timestamp('2026-09-15'), priceclose=7.0, adjustmentfactor=1.0)])
    for c in VALUE_COLS:
        if c not in s.columns: s[c] = np.nan
    m = match_rates(s).iloc[0]
    check('n_both=2, 1e-6:1, 1e-4:2, 1e-3:2, ciq_only=1', (m['match_n_both'], m['match_1e6'], m['match_1e4'], m['match_1e3'], m['ciq_close_only']) == (2, 1, 2, 2, 1))
    print('SELFTEST', 'PASS' if ok else 'FAIL')
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--once', action='store_true', help='관측 1회 실행')
    ap.add_argument('--no-full', action='store_true', help='전체(22,871 id) 집계 생략')
    ap.add_argument('--out-dir', default=OUT_DIR)
    ap.add_argument('--now', help='관측 시각 강제(ISO, tz 포함; 테스트용)')
    ap.add_argument('--selftest', action='store_true')
    ap.add_argument('--calendar', action='store_true', help='현재 시각의 T0/T-1/T-2/T+1 출력')
    ap.add_argument('--done-after', nargs=2, metavar=('START_ISO', 'N'), help='시작 시각(ISO, KST) 이후 N 미국 거래일이 지나고 그 마감+24h 지났으면 1, 아니면 0 출력')
    a = ap.parse_args()
    if a.selftest:
        sys.exit(0 if selftest() else 1)
    now = dt.datetime.fromisoformat(a.now) if a.now else dt.datetime.now(tz=KST)
    if now.tzinfo is None: now = now.replace(tzinfo=KST)
    if a.calendar:
        t0, t1, t2, tn = target_days(now.astimezone(ET))
        print(json.dumps(dict(now_kst=now.astimezone(KST).isoformat(), now_et=now.astimezone(ET).isoformat(), T0=str(t0), T_1=str(t1), T_2=str(t2), T_next=str(tn),
                              hours_since_T0_close=round((now.astimezone(ET) - close_dt(t0)).total_seconds() / 3600, 2)), indent=1))
        return
    if a.done_after:
        start = dt.datetime.fromisoformat(a.done_after[0]); n = int(a.done_after[1])
        if start.tzinfo is None: start = start.replace(tzinfo=KST)
        d_end = nth_trading_day_after(target_days(start.astimezone(ET))[0], n)
        done = now.astimezone(ET) >= close_dt(d_end) + dt.timedelta(hours=24)
        print(1 if done else 0, file=sys.stdout); print(f'end_day={d_end} finish_at_ET={close_dt(d_end) + dt.timedelta(hours=24):%Y-%m-%d %H:%M}', file=sys.stderr)
        return
    if a.once:
        def log(msg): print(msg, flush=True)
        observe(a.out_dir, full=not a.no_full, now=now, log=log)
        return
    ap.print_help()


if __name__ == '__main__':
    main()
