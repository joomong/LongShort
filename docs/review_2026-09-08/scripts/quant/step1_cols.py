import pyarrow.parquet as pq, numpy as np, pandas as pd, time, json, os
t0=time.time()
P='/home/base/cache/cache_v1_cash_1d/'
f=pq.ParquetFile(P+'TopUniverseV1/Top500.parquet')
tb=f.read()
print('read Top500 %.1fs nbytes=%.0fMB'%(time.time()-t0, tb.nbytes/1e6))
idx=pd.to_datetime(tb.column('__index_level_0__').to_numpy())
names=[n for n in tb.schema.names if n!='__index_level_0__']
START=pd.Timestamp('2015-01-01'); LOOK=pd.Timestamp('2013-09-01')
rmask=(idx>=START)
print('rows total',len(idx),'rows>=2015',rmask.sum(),'first',idx[0],'last',idx[-1])
# ever True in period
sel=[]
arrs={}
for n in names:
    a=tb.column(n).to_numpy(zero_copy_only=False)
    a=np.asarray(a)
    if a.dtype==object:
        a=np.array([bool(x) if x is not None else False for x in a])
    v=a[rmask]
    if v.any(): sel.append(n)
print('ever-True cols since 2015:',len(sel),'elapsed %.1f'%(time.time()-t0))
# save mask matrix for selected cols over full rows
M=np.zeros((len(idx),len(sel)),dtype=bool)
for j,n in enumerate(sel):
    a=tb.column(n).to_numpy(zero_copy_only=False)
    if a.dtype==object: a=np.array([bool(x) if x is not None else False for x in a])
    M[:,j]=a
np.save('top500_mask.npy',M)
np.save('dates.npy',idx.values)
json.dump(sel,open('cols.json','w'))
daily=M[rmask].sum(1)
print('daily True count: min %d med %d max %d'%(daily.min(),np.median(daily),daily.max()))
print('total %.1fs'%(time.time()-t0))
