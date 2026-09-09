import numpy as np
rng=np.random.default_rng(3)
T,N=20000,17
# persistent predictor x (AR1, halflife 5d), returns driven by it
x=np.zeros((T,N)); lam=0.5**(1/5)
for t in range(1,T): x[t]=lam*x[t-1]+np.sqrt(1-lam**2)*rng.standard_normal(N)
r=0.006*x+0.04*rng.standard_normal((T,N)); r-=r.mean(axis=1,keepdims=True)
close=np.cumprod(1+r,axis=0)*100
hr=np.vstack([np.zeros((1,N)), close[1:]/close[:-1]-1])
mid=np.vstack([close[0:1], np.sqrt(close[1:]*close[:-1])]); tr=close/mid-1
def norm(a):
    d=np.abs(a).sum(axis=1,keepdims=True); d[d<1e-12]=np.inf; return a/d
tgt=norm(x)                                  # signal known at bar t
def pnl_at_lag(k):   # weight tgt[t-k] earns hr[t]
    return np.array([ (tgt[t-k]*hr[t]).sum() if t>=k else 0. for t in range(T)])
mu1=pnl_at_lag(1).mean()*365*100
mu2=pnl_at_lag(2).mean()*365*100
pos=np.vstack([np.zeros((1,N)),tgt[:-1]])    # team pos[t]=tgt[t-1] (info<=t-1)
dpos=np.vstack([pos[0:1],np.diff(pos,axis=0)])
hold=np.array([(pos[t-1]*hr[t]).sum() if t>0 else 0. for t in range(T)])
trade=np.array([(dpos[t]*tr[t]).sum() for t in range(T)])
print(f"mu(lag1)={mu1:7.3f}%/y  mu(lag2)={mu2:7.3f}%/y   1-bar decay={mu1-mu2:7.3f}")
print(f"engine hold ann={hold.mean()*365*100:7.3f}%  (== mu lag2)")
print(f"trade leg   ann={trade.mean()*365*100:7.3f}%   predicted 0.5*(mu1-mu2)={0.5*(mu1-mu2):7.3f}")
print(f"team total  ann={(hold+trade).mean()*365*100:7.3f}%   predicted 0.5*(mu1+mu2)={0.5*(mu1+mu2):7.3f}")
for k in (1,2,3,4): print(f"  mu(lag{k})={pnl_at_lag(k).mean()*365*100:8.3f}%/y")
print(f"turnover/day={np.abs(dpos).sum(axis=1).mean():.3f}")
