import numpy as np, pandas as pd
d=np.load('panel.npz',allow_pickle=True); M=np.load('mask_slice.npy')
dates=pd.to_datetime(d['dates']); cols=d['cols']
close,mid,vwap,op,hi,lo=d['close'],d['midPrice'],d['VWAP'],d['open'],d['high'],d['low']
hr,tr,vcr=d['hr'],d['tr'],d['vcr']
ev=dates>=pd.Timestamp('2015-01-01')
valid = M & np.isfinite(close)
V = valid & ev[:,None]
print('eval cells: %d (T=%d, avg N=%.1f)'%(V.sum(), ev.sum(), V.sum()/ev.sum()))

def cmp(name,a,b,V):
    m=V & np.isfinite(a) & np.isfinite(b)
    diff=np.abs(a-b)[m]
    print('%-34s n=%d  frac|d|>1e-6=%.4f%%  frac>1e-4=%.4f%%  med=%.2e  p99.9=%.2e  max=%.3e'
          %(name,m.sum(),100*(diff>1e-6).mean(),100*(diff>1e-4).mean(),np.median(diff),np.quantile(diff,0.999),diff.max()))
    return m,diff

# (a) HoldingReturn vs close ratio
cr=np.full_like(close,np.nan); cr[1:]=close[1:]/close[:-1]-1.0
m,diff=cmp('hr vs close[t]/close[t-1]-1',hr,cr,V)
ii,jj=np.where(m); big=np.argsort(-diff)[:8]
print(' top diffs:')
for k in big:
    i,j=ii[big.tolist().index(k)] if False else (ii[k],jj[k])
    print('   %s sec=%s close[t-1]=%.4f close[t]=%.4f hr=%.5f cr=%.5f'%(dates[i].date(),cols[j],close[i-1,j],close[i,j],hr[i,j],cr[i,j]))
# (b) TradingReturn vs close/mid-1
cmp('tr vs close[t]/midPrice[t]-1',tr,close/mid-1.0,V)
cmp('tr vs midPrice[t]/close[t-1]-1',tr,np.vstack([np.full((1,close.shape[1]),np.nan),mid[1:]/close[:-1]-1.0]),V)
# (c) VWAPToClose
cmp('vcr vs close[t]/VWAP[t]-1',vcr,close/vwap-1.0,V)
# midPrice construction
cmp('midPrice vs (o+h+l+c)/4',mid,(op+hi+lo+close)/4.0,V)
# zero-fill trap check
print('\n0-fill: tr==0 exactly, inside valid %.3f%% / outside valid %.3f%%'%(100*(tr[V]==0).mean(),100*(tr[~V]==0).mean()))
print('hr NaN inside valid %.4f%%'%(100*np.isnan(hr[V]).mean()))
print('hr==0 exactly inside valid %.4f%% / outside %.2f%%'%(100*(hr[V]==0).mean(),100*(hr[~V]==0).mean()))
# scale of tr
x=tr[V]; print('\ntr stats(valid): mean=%.6f std=%.5f  |  cr stats: mean=%.6f std=%.5f'%(x.mean(),x.std(),cr[V&np.isfinite(cr)].mean(),cr[V&np.isfinite(cr)].std()))
print('corr(tr[t], hr[t]) valid = %.4f'%np.corrcoef(tr[V&np.isfinite(hr)],hr[V&np.isfinite(hr)])[0,1])
