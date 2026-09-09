import pyarrow.parquet as pq, numpy as np, pandas as pd, time, json
P='/home/base/cache/cache_v1_cash_1d/'
cols=json.load(open('cols.json')); idx=pd.to_datetime(np.load('dates.npy'))
LOOK=pd.Timestamp('2013-06-01')          # lookback buffer for 252d momentum
rsel=np.where(idx>=LOOK)[0]; o=int(rsel[0]); L=len(rsel)
print('slice rows',o,L,idx[o],idx[-1])
fields={'close':'BaseData/close','midPrice':'BaseData/midPrice','VWAP':'BaseData/VWAP',
        'open':'BaseData/open','high':'BaseData/high','low':'BaseData/low',
        'hr':'Returns/HoldingReturn_Usd','tr':'Returns/TradingReturn_Usd','vcr':'Returns/VWAPToCloseReturn_Usd'}
out={}
tot=0
for k,rel in fields.items():
    t0=time.time()
    tb=pq.read_table(P+rel+'.parquet',columns=cols)
    A=np.empty((L,len(cols)),dtype=np.float64)
    for j,n in enumerate(cols):
        A[:,j]=tb.column(n).to_numpy(zero_copy_only=False)[o:o+L]
    out[k]=A; del tb
    dt=time.time()-t0; tot+=dt
    print('%-10s %.1fs shape%s nan%%=%.1f'%(k,dt,A.shape,100*np.isnan(A).mean()))
np.savez('panel.npz', dates=idx[o:o+L].values, cols=np.array(cols), **out)
print('mask slice'); M=np.load('top500_mask.npy')[o:o+L]
np.save('mask_slice.npy',M)
print('total load %.1fs'%tot)
