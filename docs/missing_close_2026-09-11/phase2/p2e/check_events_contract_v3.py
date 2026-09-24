"""Lead's independent check of the events-file contract v3 invariants (BRIEF Phase 2e §E-1)."""
import sys, pyarrow.parquet as pq, pandas as pd, numpy as np
path=sys.argv[1] if len(sys.argv)>1 else 'data/events/events.parquet'
ev=pq.read_table(path).to_pandas()
need=['hasCashLeg','hasStockLeg','cashPerShareCache','successorRatioCache','cacheUnitStatus','targetCacheMark','targetCacheMarkDate','successorCacheClose','successorCacheCloseDate','settlementReturn','successorRatio','successorTradingItemId','cashPerShare','endDate']
miss=[c for c in need if c not in ev.columns]
fails=[]
if miss: fails.append(f'missing columns {miss}')
else:
    cc=ev['cashPerShareCache'].notna(); rc=ev['successorRatioCache'].notna()
    hs=ev['hasStockLeg'].astype(bool); hc=ev['hasCashLeg'].astype(bool)
    i1=(hs & ~rc & cc).sum(); i2=(hc & ~cc & rc).sum()
    i3=(rc & (ev['successorCacheClose'].isna() | ev['successorCacheCloseDate'].isna())).sum()
    i4=((cc|rc) & (ev['targetCacheMark'].isna() | ev['targetCacheMarkDate'].isna())).sum()
    ok=ev['cacheUnitStatus'].eq('ok'); i6=(ok != (cc|rc)).sum()
    # ratio without successor id must not be dropped: raw ratio kept when text has stock leg is generator-side; check structure flag
    i7=(hs & ev['successorRatio'].isna() & ev['successorTradingItemId'].isna()).sum()  # informational
    # identity (5)
    m=rc | cc
    V=ev['cashPerShareCache'].fillna(0)+ev['successorRatioCache'].fillna(0)*ev['successorCacheClose'].fillna(0)
    R=V/ev['targetCacheMark']-1
    i5=((R-ev['settlementReturn']).abs()>1e-6)[m & ev['settlementReturn'].notna()].sum()
    # settlement bar check for successor guard date against cache
    pf=pq.ParquetFile('/home/base/cache/cache_v1_cash_1d/BaseData/close.parquet'); names=set(pf.schema.names)
    sub=ev[rc]; tids=[t for t in sub['tradingItemId'].astype(str).unique() if t in names]
    close=pf.read(columns=['__index_level_0__']+tids).to_pandas(); close.columns=close.columns.astype(str); close.index=pd.to_datetime(close.index)
    bad_date=0; bad_mark=0
    for _,r in sub.iterrows():
        t=str(r['tradingItemId'])
        if t not in close.columns: continue
        s=close[t]; after=s[s.index>r['endDate']]; na=after[after.isna()]
        sb=na.index[0] if len(na) else pd.NaT
        if pd.isna(sb) or pd.Timestamp(r['successorCacheCloseDate']).normalize()!=sb: bad_date+=1
        lv=s[s.notna()]; 
        if len(lv)==0 or pd.Timestamp(r['targetCacheMarkDate']).normalize()!=lv.index.max() or not np.isclose(r['targetCacheMark'],lv.iloc[-1],rtol=1e-6): bad_mark+=1
    for name,val in [('①stock leg w/o ratioCache but cashCache',i1),('②cash leg w/o cashCache but ratioCache',i2),('③ratioCache w/o successor guard',i3),('④eval inputs w/o target guard',i4),('⑤identity mismatch',i5),('⑥status!=eval',i6),('③b successor guard date != settlement bar',bad_date),('④b target guard != last valid cache close',bad_mark)]:
        print(f'{name}: {val}'); 
        if val: fails.append(name)
    print('info: hasStockLeg w/o ratio and w/o successor id:', i7, '| status counts:', ev['cacheUnitStatus'].value_counts().to_dict())
    print('rows', len(ev), 'eval rows', int(m.sum()), 'hasStockLeg', int(hs.sum()), 'hasCashLeg', int(hc.sum()))
print('RESULT', 'FAIL '+'; '.join(fails) if fails else 'PASS')
sys.exit(1 if fails else 0)
